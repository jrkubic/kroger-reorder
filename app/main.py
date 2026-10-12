"""FastAPI app: serves the page and a small JSON API over Storage + KrogerClient."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .kroger import KrogerError, NotSignedIn
from .storage import Item, Storage

STATIC_DIR = Path(__file__).resolve().parent / "static"


class StoreChoice(BaseModel):
    id: str
    name: str


class NewItem(BaseModel):
    upc: str
    name: str
    size: str = ""


class ItemModel(BaseModel):
    upc: str
    name: str
    size: str = ""
    quantity: int = 1
    included: bool = True


def create_app(storage: Storage, kroger) -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake)."""
    app = FastAPI(title="Kroger Reorder")

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(_: Request, exc: NotSignedIn):
        return JSONResponse(status_code=401, content={"error": exc.message, "signed_in": False})

    @app.exception_handler(KrogerError)
    async def kroger_error(_: Request, exc: KrogerError):
        return JSONResponse(status_code=502, content={"error": f"Kroger said: {exc.message}"})

    def error(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": message, **extra})

    def items_payload() -> dict:
        loaded = storage.load_list()
        return {"items": [asdict(i) for i in loaded.items], "warning": loaded.warning}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        config = storage.load_config()
        return {"store": config or None, "signed_in": kroger.is_signed_in(), **items_payload()}

    @app.get("/api/stores")
    def stores(zip: str):
        return {"stores": kroger.find_stores(zip)}

    @app.put("/api/store")
    def choose_store(choice: StoreChoice):
        storage.save_config({"store_id": choice.id, "store_name": choice.name})
        return {"store": storage.load_config()}

    @app.get("/auth/login")
    def login():
        return RedirectResponse(kroger.authorize_url())

    @app.get("/callback")
    def callback(code: str):
        kroger.exchange_code(code)
        return RedirectResponse("/")

    @app.get("/api/search")
    def search(term: str):
        config = storage.load_config()
        if not config.get("store_id"):
            return error(400, "Pick a store first.")
        return {"results": kroger.search_products(term, config["store_id"])}

    return app
