"""Start Baya.io Companion: ``python -m companion``.

Binds only to this PC's local-network address, prints the pairing PIN and
shows a small always-on-top status window on the PC while it runs.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

from . import config
from .assistant import Assistant, OllamaGenerator
from .library import DocumentLibrary
from .pipeline import WhisperService, make_pipeline_factory
from .security import Authenticator, is_local_client
from .server import create_app
from .session import EventBus, Session


def local_ip() -> str:
    """The address of the interface used for the local network (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def status_window(session: Session, url: str, auth: Authenticator) -> None:
    """Small PC-side indicator, so capture is visible on the computer as well as on the iPad."""
    import tkinter as tk

    root = tk.Tk()
    root.title("Baya.io Companion")
    root.geometry("360x150+40+40")
    root.attributes("-topmost", True)
    root.configure(bg="#ffffff")
    state = tk.Label(root, font=("Segoe UI", 16, "bold"), bg="#ffffff")
    state.pack(fill="x", padx=16, pady=(14, 2))
    info = tk.Label(root, font=("Segoe UI", 10), bg="#ffffff", fg="#6b6880", justify="left")
    info.pack(fill="x", padx=16)

    def refresh() -> None:
        listening = session.state == "listening"
        state.configure(text="● LISTENING" if listening else "○ Capture off",
                        fg="#e5484d" if listening else "#6b6880")
        root.configure(bg="#fdecec" if listening else "#ffffff")
        for widget in (state, info):
            widget.configure(bg="#fdecec" if listening else "#ffffff")
        info.configure(text=f"Open on the iPad:  {url}\nPairing PIN:  {auth.pin}")
        root.after(300, refresh)

    refresh()
    root.protocol("WM_DELETE_WINDOW", lambda: (session.stop(), root.destroy()))
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baya.io Companion: local second screen for authorized meetings.")
    parser.add_argument("--host", help="local-network address to bind (default: detected automatically)")
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-window", action="store_true", help="do not show the PC status window")
    parser.add_argument("--no-llm", action="store_true", help="show document excerpts only, without summaries")
    args = parser.parse_args(argv)

    host = args.host or local_ip()
    if not is_local_client(host):
        print(f"Refusing to start: {host} is not a private local-network address.", file=sys.stderr)
        return 2

    import uvicorn

    library = DocumentLibrary(config.DATA_DIR)
    auth = Authenticator()
    bus = EventBus()
    generator = None if args.no_llm else OllamaGenerator()
    whisper = WhisperService()
    session = Session(library, bus, make_pipeline_factory(whisper), Assistant(library, generator))
    app = create_app(library, session, bus, auth)
    url = f"http://{host}:{args.port}"
    auth.on_pin_rotated = lambda pin: print(f"\n[security] Too many failed logins. New PIN: {pin}\n", flush=True)

    print("\n  Baya.io Companion")
    print(f"  Open on the iPad (same Wi-Fi):  {url}")
    print(f"  Pairing PIN:                    {auth.pin}")
    print(f"  Documents folder:               {library.root}")
    print("  Capture is OFF until you press Start on the iPad. Ctrl+C to quit.\n", flush=True)

    def warm_up() -> None:  # load models in the background so Start is quick
        try:
            whisper.model(lambda msg: print(f"  {msg}", flush=True))
            print("  Speech model ready.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  Speech model not loaded yet: {exc}", flush=True)
        if generator is not None:
            try:
                generator.warm_up()
                print(f"  Local model ready ({generator.model}).", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  Ollama not reachable ({exc}). Excerpts will be shown without summaries.", flush=True)

    threading.Thread(target=warm_up, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=args.port, log_level="warning",
                                           proxy_headers=False, server_header=False))
    if args.no_window:
        server.run()
        return 0
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        status_window(session, url, auth)
    except Exception:  # noqa: BLE001 - no display available: keep serving in the console
        while thread.is_alive():
            time.sleep(0.5)
    server.should_exit = True
    session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
