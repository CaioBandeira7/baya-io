"""HTTP API: authentication on every route (SSE included), CSRF guard, documents and session."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from companion.assistant import Assistant
from companion.library import DocumentLibrary
from companion.security import COOKIE_NAME, Authenticator
from companion.server import create_app
from companion.session import EventBus, Session

LAN = ("192.168.0.20", 51000)
ORIGIN = {"Origin": "http://testserver"}


class FakePipeline:
    def __init__(self, source, on_turn, on_status):
        self.source, self.on_turn, self.on_status = source, on_turn, on_status
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def env(tmp_path):
    library = DocumentLibrary(tmp_path)
    auth = Authenticator(pin="246810", max_attempts=3)
    bus = EventBus()
    pipelines = []

    def factory(source, on_turn, on_status):
        pipelines.append(FakePipeline(source, on_turn, on_status))
        return pipelines[-1]

    session = Session(library, bus, factory, Assistant(library, generator=None, min_terms=1, min_score=0))
    app = create_app(library, session, bus, auth, heartbeat_s=0.05)
    client = TestClient(app, client=LAN)
    return {"client": client, "library": library, "session": session, "auth": auth, "pipelines": pipelines, "app": app}


def login(client):
    response = client.post("/api/login", json={"pin": "246810"}, headers=ORIGIN)
    assert response.status_code == 200
    return response


# --- Network and authentication ----------------------------------------------------------

def test_public_internet_clients_are_refused(env):
    outsider = TestClient(env["app"], client=("8.8.8.8", 4000))
    for path in ("/login", "/api/state", "/api/events", "/static/app.js"):
        assert outsider.get(path).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/state"), ("GET", "/api/documents"), ("GET", "/api/events"), ("GET", "/static/app.js"),
    ("GET", "/static/index.html"), ("POST", "/api/session/start"), ("POST", "/api/session/stop"),
    ("PATCH", "/api/documents/x"), ("DELETE", "/api/documents/x"), ("POST", "/api/documents"),
])
def test_every_route_requires_authentication(env, method, path):
    response = env["client"].request(method, path, headers=ORIGIN, json={})
    assert response.status_code == 401


def test_pages_redirect_to_login_and_login_page_is_public(env):
    client = env["client"]
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    page = client.get("/login")
    assert page.status_code == 200 and "Pair this device" in page.text
    assert page.headers["x-frame-options"] == "DENY" and "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/login.html").status_code in (401, 404)  # template is never served raw


def test_login_sets_strict_httponly_cookie_and_unlocks_routes(env):
    client = env["client"]
    cookie = login(client).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/state").status_code == 200
    assert client.get("/").status_code == 200
    client.post("/api/logout", headers=ORIGIN)
    assert client.get("/api/state").status_code == 401


def test_login_rate_limit(env):
    client = env["client"]
    for _ in range(3):
        assert client.post("/api/login", json={"pin": "000000"}, headers=ORIGIN).status_code == 401
    locked = client.post("/api/login", json={"pin": "246810"}, headers=ORIGIN)
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0


def test_cross_site_requests_are_blocked_even_when_logged_in(env):
    client = env["client"]
    login(client)
    assert client.post("/api/session/stop").status_code == 403                       # no Origin
    assert client.post("/api/session/stop", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/session/stop", headers={**ORIGIN, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/session/stop", headers=ORIGIN).status_code == 200


def test_sse_stream_requires_auth_and_sends_snapshot(env):
    import threading

    client = env["client"]
    assert client.get("/api/events").status_code == 401
    login(client)
    # The test client buffers the whole body, so end the stream by revoking the session:
    # this also checks that a revoked device loses the live feed.
    threading.Timer(0.3, env["auth"].revoke_all).start()
    response = client.get("/api/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [l for l in response.text.splitlines() if l.startswith("event:")]
    assert events[0] == "event: snapshot"
    snapshot = json.loads(next(l for l in response.text.splitlines() if l.startswith("data:"))[5:])
    assert snapshot["state"] == "idle" and snapshot["documents"] == []
    assert ": keep-alive" in response.text


# --- Documents ------------------------------------------------------------------------------

def test_upload_toggle_and_delete_document(env, civil_pdf):
    client = env["client"]
    login(client)
    created = client.post("/api/documents", files={"file": ("civil.pdf", civil_pdf, "application/pdf")}, headers=ORIGIN)
    assert created.status_code == 201
    doc = created.json()["document"]
    assert "sha256" not in doc and doc["chunk_count"] == 3
    again = client.post("/api/documents", files={"file": ("copy.pdf", civil_pdf, "application/pdf")}, headers=ORIGIN)
    assert again.status_code == 200 and again.json()["created"] is False

    off = client.patch(f"/api/documents/{doc['id']}", json={"active": False}, headers=ORIGIN)
    assert off.json()["active"] is False
    assert client.delete(f"/api/documents/{doc['id']}", headers=ORIGIN).status_code == 200
    assert client.get("/api/documents").json() == []
    assert client.delete(f"/api/documents/{doc['id']}", headers=ORIGIN).status_code == 404


def test_bad_uploads_return_readable_errors(env):
    client = env["client"]
    login(client)
    bad = client.post("/api/documents", files={"file": ("x.pdf", b"not a pdf", "application/pdf")}, headers=ORIGIN)
    assert bad.status_code == 422 and "not a valid PDF" in bad.json()["detail"]
    exe = client.post("/api/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=ORIGIN)
    assert exe.status_code == 422


def test_upload_size_limit(tmp_path, env):
    env["library"].max_bytes = 50
    client = env["client"]
    login(client)
    big = client.post("/api/documents", files={"file": ("big.txt", b"word " * 100, "text/plain")}, headers=ORIGIN)
    assert big.status_code == 413


# --- Session ---------------------------------------------------------------------------------

def test_capture_is_off_until_start_and_stop_clears(env, civil_pdf):
    client, session = env["client"], env["session"]
    login(client)
    assert client.get("/api/state").json()["state"] == "idle"
    assert env["pipelines"] == []                      # nothing created before Start
    session.add_turn("system", "ignored while idle", time.perf_counter())
    assert session.transcript == []

    assert client.post("/api/session/start", json={"source": "bogus"}, headers=ORIGIN).status_code == 422
    started = client.post("/api/session/start", json={"source": "both"}, headers=ORIGIN).json()
    assert started["state"] == "listening" and env["pipelines"][0].source == "both"

    session.add_turn("system", "What does article 927 say about repairing damage?", time.perf_counter())
    assert client.get("/api/state").json()["transcript"][0]["text"].startswith("What does")

    stopped = client.post("/api/session/stop", headers=ORIGIN).json()
    assert stopped["state"] == "idle" and stopped["transcript"] == [] and stopped["context"] == []
    assert env["pipelines"][0].stopped


def test_default_source_is_system_audio(env):
    client = env["client"]
    login(client)
    client.post("/api/session/start", json={}, headers=ORIGIN)
    assert env["pipelines"][0].source == "system"
