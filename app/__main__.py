# app/__main__.py
"""`python -m app` (or `kroger-reorder`): start the server for the whole LAN and open the browser.

`python -m app --no-browser` skips opening the browser; use it for the Windows startup entry
(see scripts/install-startup.ps1). When launched with pythonw.exe there is no console, so output
goes to %LOCALAPPDATA%\\kroger-reorder\\server.log instead.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .kroger import KrogerClient
from .main import create_app
from .settings import load_settings
from .storage import Storage


def lan_ip() -> str | None:
    """Best-effort LAN address: the source IP the OS would use to reach the internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packets are sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "kroger-reorder" / "server.log"


def attach_log_when_headless() -> None:
    """Under pythonw.exe stdout/stderr are None; send output to a log file so errors are not lost."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered; closed when the process exits
    sys.stdout = sys.stderr = log


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kroger-reorder", description="Run the Kroger Reorder server.")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in a browser at startup")
    parser.add_argument("--launcher", action="store_true",
                        help="Start Menu behaviour: if the server is already running just open the page, else start it")
    args = parser.parse_args(argv)
    attach_log_when_headless()

    settings = load_settings()
    local = f"http://localhost:{settings.port}"
    if args.launcher and port_in_use(settings.port):
        webbrowser.open(local)
        return
    storage = Storage()
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    app = create_app(storage, kroger, photo_model=settings.photo_model,
                     photo_timeout=settings.photo_timeout)  # also migrates v1 data and ensures a default list
    if port_in_use(settings.port):
        sys.exit(
            f"Port {settings.port} is already in use (is Kroger Reorder already running?). "
            "Close it, or set PORT in .env and update the redirect URL on your Kroger app."
        )
    print(f"Kroger Reorder running at {local}", flush=True)
    if ip := lan_ip():
        print(f"  On your wifi:  http://{ip}:{settings.port}", flush=True)
    print("  Sign in to Kroger from this computer; other devices share the sign-in.  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(local,)).start()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
