"""Pairing PIN, lockout, session expiry and the local-network filter."""

from __future__ import annotations

import pytest

from companion.security import Authenticator, is_local_client


@pytest.mark.parametrize("host,allowed", [
    ("127.0.0.1", True), ("192.168.0.23", True), ("10.1.2.3", True), ("172.20.4.5", True),
    ("::1", True), ("fd00::5", True), ("::ffff:192.168.1.9", True),
    ("8.8.8.8", False), ("172.32.0.1", False), ("100.64.0.1", False), ("169.254.10.10", False),
    ("2001:4860:4860::8888", False), ("0.0.0.0", False), ("testclient", False), (None, False), ("", False),
])
def test_local_network_filter(host, allowed):
    assert is_local_client(host) is allowed


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_pin_login_issues_valid_session_and_logout_revokes():
    auth = Authenticator(pin="123456")
    token = auth.login("192.168.0.5", "123456")
    assert token and auth.is_valid(token)
    auth.logout(token)
    assert not auth.is_valid(token)


def test_wrong_pin_locks_out_client_then_recovers():
    clock = Clock()
    auth = Authenticator(pin="123456", max_attempts=3, lockout_seconds=60, clock=clock)
    for _ in range(3):
        assert auth.login("192.168.0.5", "000000") is None
    with pytest.raises(PermissionError):
        auth.login("192.168.0.5", "123456")      # even the right PIN is refused while locked
    assert auth.login("192.168.0.6", "123456")   # other devices are not affected
    clock.now += 61
    assert auth.login("192.168.0.5", "123456")


def test_many_failures_across_devices_rotate_the_pin():
    rotated = []
    auth = Authenticator(pin="123456", max_attempts=100, max_global_failures=5)
    auth.on_pin_rotated = rotated.append
    for i in range(5):
        auth.login(f"192.168.0.{i + 10}", "999999")
    assert len(rotated) == 1 and rotated[0] == auth.pin
    if auth.pin != "123456":                     # 1-in-a-million chance the new PIN is identical
        assert auth.login("192.168.0.50", "123456") is None


def test_sessions_expire():
    clock = Clock()
    auth = Authenticator(pin="123456", session_seconds=10, clock=clock)
    token = auth.login("192.168.0.5", "123456")
    clock.now += 11
    assert not auth.is_valid(token)


def test_garbage_tokens_are_rejected():
    auth = Authenticator(pin="123456")
    assert not auth.is_valid(None) and not auth.is_valid("") and not auth.is_valid("x" * 43)
