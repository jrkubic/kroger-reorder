"""FastAPI app: serves the page and a small JSON API over Storage + KrogerClient."""
from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
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

    def error(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": message, **extra})

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(_: Request, exc: NotSignedIn):
        return error(401, exc.message, signed_in=False)

    @app.exception_handler(KrogerError)
    async def kroger_error(_: Request, exc: KrogerError):
        return error(502, f"Kroger said: {exc.message}")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error(422, f"Invalid request: {loc or 'body'} {first.get('msg', '')}".strip())

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
    def callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error or not code:
            return RedirectResponse(f"/?auth_error={quote(error or 'no code returned')}")
        try:
            kroger.exchange_code(code, state)
        except KrogerError as exc:
            return RedirectResponse(f"/?auth_error={quote(exc.message)}")
        return RedirectResponse("/")

    @app.get("/api/search")
    def search(term: str):
        config = storage.load_config()
        if not config.get("store_id"):
            return error(400, "Pick a store first.")
        return {"results": kroger.search_products(term, config["store_id"])}

    @app.post("/api/items")
    def add_item(new: NewItem):
        items = storage.load_list().items
        if not any(i.upc == new.upc for i in items):
            items.append(Item(upc=new.upc, name=new.name, size=new.size))
            storage.save_list(items)
        return items_payload()

    @app.put("/api/items")
    def replace_items(models: list[ItemModel]):
        if models:
            storage.save_list([Item(**m.model_dump()) for m in models])
        return items_payload()

    @app.delete("/api/items/{upc}")
    def remove_item(upc: str):
        items = storage.load_list().items
        kept = [i for i in items if i.upc != upc]
        if len(kept) != len(items):
            storage.save_list(kept)
        return items_payload()

    @app.post("/api/push")
    def push(models: list[ItemModel]):
        items = [Item(**m.model_dump()) for m in models]
        if models:
            storage.save_list(items)
        chosen = [i for i in items if i.included and i.quantity > 0]
        if not chosen:
            return error(400, "Nothing is checked to send.")
        try:
            kroger.add_to_cart([(i.upc, i.quantity) for i in chosen])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            # Whole-token match so "1111041" is not flagged when Kroger rejects "0001111041700".
            bad = [i.upc for i in chosen if re.search(rf"(?<!\d){re.escape(i.upc)}(?!\d)", exc.message)]
            return error(502, f"Kroger said: {exc.message}", bad_upcs=bad)
        return {"added": [asdict(i) for i in chosen]}

    return app
