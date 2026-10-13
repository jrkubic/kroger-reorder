# app/__main__.py
"""`python -m app` (or `kroger-reorder`): start the server for the whole LAN and open the browser."""
from __future__ import annotations

import socket
import sys
import threading
import webbrowser

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


def main() -> None:
    settings = load_settings()
    storage = Storage()
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    app = create_app(storage, kroger)  # also migrates v1 data and ensures a default list
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", settings.port))
        except OSError:
            sys.exit(
                f"Port {settings.port} is already in use (is Kroger Reorder already running?). "
                "Close it, or set PORT in .env and update the redirect URL on your Kroger app."
            )
    local = f"http://localhost:{settings.port}"
    print(f"Kroger Reorder running at {local}", flush=True)
    if ip := lan_ip():
        print(f"  On your wifi:  http://{ip}:{settings.port}", flush=True)
    print("  Sign in to Kroger from this computer; other devices share the sign-in.  (Ctrl+C to stop)", flush=True)
    threading.Timer(1.0, webbrowser.open, args=(local,)).start()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
