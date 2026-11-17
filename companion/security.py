"""Pairing PIN, session cookies, local-network filter and login rate limiting."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass, field

from . import config

COOKIE_NAME = "baya_session"


def is_local_client(host: str | None) -> bool:
    """Allow only loopback and private (RFC 1918 / ULA) addresses."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_loopback or (address.is_private and not address.is_link_local and not address.is_unspecified)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class Authenticator:
    """In-memory pairing: a 6-digit PIN printed on the PC exchanges for a session cookie."""

    session_seconds: int = config.SESSION_HOURS * 3600
    max_attempts: int = config.MAX_PIN_ATTEMPTS_PER_IP
    lockout_seconds: int = config.LOCKOUT_SECONDS
    max_global_failures: int = config.MAX_FAILED_ATTEMPTS_GLOBAL
    pin: str = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")
    clock: callable = time.monotonic

    def __post_init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._attempts: dict[str, _Attempts] = {}
        self._global_failures = 0
        self._lock = threading.Lock()
        self.on_pin_rotated = None

    def rotate_pin(self) -> str:
        self.pin = f"{secrets.randbelow(10**6):06d}"
        self._global_failures = 0
        if self.on_pin_rotated:
            self.on_pin_rotated(self.pin)
        return self.pin

    def retry_after(self, client: str) -> int:
        with self._lock:
            state = self._attempts.get(client)
            if state and state.locked_until > self.clock():
                return int(state.locked_until - self.clock()) + 1
            return 0

    def login(self, client: str, pin: str) -> str | None:
        """Return a new session token, or None. Raises PermissionError while locked out."""
        with self._lock:
            now = self.clock()
            state = self._attempts.setdefault(client, _Attempts())
            if state.locked_until > now:
                raise PermissionError(int(state.locked_until - now) + 1)
            if secrets.compare_digest(str(pin).strip(), self.pin):
                self._attempts.pop(client, None)
                token = secrets.token_urlsafe(32)
                self._sessions[_hash(token)] = now + self.session_seconds
                return token
            state.failures += 1
            self._global_failures += 1
            if state.failures >= self.max_attempts:
                state.failures, state.locked_until = 0, now + self.lockout_seconds
            rotate = self._global_failures >= self.max_global_failures
        if rotate:  # many failures from many devices: the PIN may be under attack
            self.rotate_pin()
        return None

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._sessions.get(_hash(token))
            if expiry is None:
                return False
            if expiry < self.clock():
                self._sessions.pop(_hash(token), None)
                return False
            return True

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_hash(token), None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    # -- one-time launch links for the PC's own browser --------------------------------
    def create_launch_token(self, ttl_seconds: int = 120) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._launch = getattr(self, "_launch", {})
            self._launch[_hash(token)] = self.clock() + ttl_seconds
        return token

    def redeem_launch_token(self, token: str) -> str | None:
        """Single use: exchange a launch token for a session token."""
        with self._lock:
            expiry = getattr(self, "_launch", {}).pop(_hash(token), None)
            if expiry is None or expiry < self.clock():
                return None
            session = secrets.token_urlsafe(32)
            self._sessions[_hash(session)] = self.clock() + self.session_seconds
            return session
