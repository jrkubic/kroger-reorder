"""`python -m app` (or `kroger-reorder`): start the local server and open the browser."""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from .kroger import KrogerClient
from .main import create_app
from .settings import load_settings
from .storage import Storage


def main() -> None:
    settings = load_settings()
    storage = Storage()
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    app = create_app(storage, kroger)
    url = f"http://localhost:{settings.port}"
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"Kroger Reorder running at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
