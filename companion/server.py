"""FastAPI application: authenticated API, SSE stream and the static iPad interface."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config
from .library import DocumentError, DocumentLibrary
from .security import COOKIE_NAME, Authenticator, is_local_client
from .session import EventBus, Session

# Data-free assets needed by the login page are the only unauthenticated routes.
PUBLIC_PATHS = {"/login", "/api/login", "/static/login.js", "/static/baya-logo.png", "/static/style.css"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"),
}


class PinBody(BaseModel):
    pin: str


class ActiveBody(BaseModel):
    active: bool


class StartBody(BaseModel):
    source: str = config.DEFAULT_AUDIO_SOURCE


def _doc_json(doc) -> dict:
    data = asdict(doc)
    data.pop("sha256", None)
    return data


def create_app(library: DocumentLibrary, session: Session, bus: EventBus, auth: Authenticator,
               static_dir: Path = config.STATIC_DIR, heartbeat_s: float = 15.0,
               host_ip: str | None = None) -> FastAPI:
    app = FastAPI(title="Baya.io Companion", docs_url=None, redoc_url=None, openapi_url=None)
    login_html = (static_dir / "login.html").read_text(encoding="utf-8")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else None
        if not is_local_client(client):
            return Response("Baya.io Companion only accepts devices on the local network.", status_code=403)
        path = request.url.path
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin != expected or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
        if (path not in PUBLIC_PATHS and not path.startswith("/launch/")
                and not auth.is_valid(request.cookies.get(COOKIE_NAME))):
            if request.method == "GET" and not path.startswith(("/api/", "/static/")):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if path.startswith(("/api/", "/launch/")) or path in ("/", "/login", "/control", "/display"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- pages -----------------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if auth.is_valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_html)

    def is_host_machine(request: Request) -> bool:
        """The browser runs on the PC itself (loopback or the server's own address)."""
        client = request.client.host if request.client else ""
        return client in {"127.0.0.1", "::1"} | ({host_ip} if host_ip else set())

    @app.get("/")
    async def index(request: Request):
        # The PC gets the control panel; other devices (the iPad) get the results display.
        return RedirectResponse("/control" if is_host_machine(request) else "/display", status_code=303)

    @app.get("/control")
    async def control():
        return FileResponse(static_dir / "index.html")

    @app.get("/display")
    async def display():
        return FileResponse(static_dir / "display.html")

    @app.get("/launch/{token}")
    async def launch(token: str, request: Request):
        """One-time link opened automatically in the PC's browser, so the PC needs no PIN."""
        session_token = auth.redeem_launch_token(token) if is_host_machine(request) else None
        if session_token is None:
            return RedirectResponse("/login", status_code=303)
        response = RedirectResponse("/control", status_code=303)
        response.set_cookie(COOKIE_NAME, session_token, max_age=auth.session_seconds, httponly=True,
                            samesite="lax", secure=request.url.scheme == "https", path="/")
        return response

    @app.get("/static/{name}")
    async def static(name: str):
        path = (static_dir / name).resolve()
        if path.parent != static_dir.resolve() or not path.is_file() or name.endswith(".html"):
            raise HTTPException(404)
        return FileResponse(path)

    # -- auth -------------------------------------------------------------------------------
    @app.post("/api/login")
    async def login(body: PinBody, request: Request):
        client = request.client.host
        try:
            token = auth.login(client, body.pin)
        except PermissionError as locked:
            return JSONResponse({"detail": f"Too many attempts. Try again in {locked.args[0]} s."},
                                status_code=429, headers={"Retry-After": str(locked.args[0])})
        if token is None:
            return JSONResponse({"detail": "Incorrect PIN."}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE_NAME, token, max_age=auth.session_seconds, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # -- state and documents --------------------------------------------------------------
    def state() -> dict:
        return {**session.snapshot(), "documents": [_doc_json(d) for d in library.list_documents()],
                "sources": list(config.AUDIO_SOURCES), "model": config.OLLAMA_MODEL,
                "max_upload_mb": config.MAX_UPLOAD_MB,
                "allowed_extensions": sorted(config.ALLOWED_EXTENSIONS)}

    @app.get("/api/state")
    async def get_state():
        return state()

    @app.get("/api/documents")
    async def list_documents():
        return [_doc_json(d) for d in library.list_documents()]

    @app.post("/api/documents")
    async def upload(file: UploadFile = File(...)):
        limit = library.max_bytes
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, f"The file is larger than {limit // (1024 * 1024)} MB.")
        try:
            doc, created = await run_in_threadpool(library.add, file.filename or "document", bytes(data))
        except DocumentError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse({"document": _doc_json(doc), "created": created}, status_code=201 if created else 200)

    @app.patch("/api/documents/{doc_id}")
    async def set_active(doc_id: str, body: ActiveBody):
        try:
            return _doc_json(await run_in_threadpool(library.set_active, doc_id, body.active))
        except KeyError:
            raise HTTPException(404, "Document not found.") from None

    @app.delete("/api/documents/{doc_id}")
    async def delete(doc_id: str):
        try:
            await run_in_threadpool(library.delete, doc_id)
        except KeyError:
            raise HTTPException(404, "Document not found.") from None
        return {"ok": True}

    # -- session ----------------------------------------------------------------------------
    @app.post("/api/session/start")
    async def start(body: StartBody):
        try:
            await run_in_threadpool(session.start, body.source)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface start-up failures to the iPad
            raise HTTPException(503, f"Could not start capture: {exc}") from exc
        return session.snapshot()

    @app.post("/api/session/stop")
    async def stop():
        await run_in_threadpool(session.stop)
        return session.snapshot()

    # -- live events --------------------------------------------------------------------------
    @app.get("/api/events")
    async def events(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        queue = bus.subscribe()

        async def stream():
            try:
                yield f"event: snapshot\ndata: {json.dumps(state(), ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected() or not auth.is_valid(token):
                        break
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
