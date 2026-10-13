# app/main.py
"""FastAPI app: serves the page and a JSON API over Storage + KrogerClient."""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .importer import parse as parse_import
from .kroger import KrogerError, NotSignedIn
from .storage import Item, ListLoad, Storage

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESOLVE_CANDIDATES = 3
TRANSPORT_FAILURE_LIMIT = 3  # consecutive status-0 errors before a price refresh gives up


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreChoice(BaseModel):
    id: str
    name: str


class ListName(BaseModel):
    name: str


class ActiveChoice(BaseModel):
    slug: str


class NewItem(BaseModel):
    upc: str
    name: str
    size: str = ""
    image: str | None = None
    price: float | None = None
    promo: float | None = None


class ItemModel(BaseModel):
    upc: str
    name: str
    size: str = ""
    quantity: int = 1
    included: bool = True
    image: str | None = None
    price: float | None = None
    promo: float | None = None
    price_checked_at: str | None = None


class ImportText(BaseModel):
    text: str


class ResolveRequest(BaseModel):
    names: list[str]


class ImportItem(BaseModel):
    upc: str
    name: str
    size: str = ""
    image: str | None = None
    quantity: int = 1


class ListNotFound(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


def create_app(storage: Storage, kroger) -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake)."""
    storage.ensure_ready()
    app = FastAPI(title="Kroger Reorder")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def error(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": message, **extra})

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(_: Request, exc: NotSignedIn):
        return error(401, exc.message, signed_in=False)

    def kroger_message(exc: KrogerError) -> str:
        # status 0 is a transport failure: Kroger never answered, so there is nothing to quote.
        return exc.message if exc.status == 0 else f"Kroger said: {exc.message}"

    @app.exception_handler(KrogerError)
    async def kroger_error(_: Request, exc: KrogerError):
        return error(502, kroger_message(exc))

    @app.exception_handler(ListNotFound)
    async def list_not_found(_: Request, exc: ListNotFound):
        return error(404, f"No list named '{exc.slug}'.")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error(422, f"Invalid request: {loc or 'body'} {first.get('msg', '')}".strip())

    # -- helpers ----------------------------------------------------------------------
    def get_list(slug: str) -> ListLoad:
        loaded = storage.load_list(slug)
        if loaded is None:
            raise ListNotFound(slug)
        return loaded

    def list_payload(loaded: ListLoad, **extra) -> dict:
        return {"slug": loaded.slug, "name": loaded.name,
                "items": [asdict(i) for i in loaded.items], "warning": loaded.warning, **extra}

    def lists_payload() -> dict:
        return {"lists": storage.list_lists(), "active_list": storage.active_list()}

    def store_id() -> str | None:
        return storage.load_config().get("store_id")

    def clean_name(raw: str) -> str | None:
        name = raw.strip()
        return name or None

    # -- page / setup -----------------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        config = storage.load_config()
        store = {"store_id": config["store_id"], "store_name": config.get("store_name", "")} if config.get("store_id") else None
        return {"store": store, "signed_in": kroger.is_signed_in(), **lists_payload()}

    @app.get("/api/stores")
    def stores(zip: str):
        return {"stores": kroger.find_stores(zip)}

    @app.put("/api/store")
    def choose_store(choice: StoreChoice):
        storage.save_config({**storage.load_config(), "store_id": choice.id, "store_name": choice.name})
        return {"store": {"store_id": choice.id, "store_name": choice.name}}

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
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        return {"results": kroger.search_products(term, sid)}

    # -- lists ------------------------------------------------------------------------------
    @app.post("/api/lists")
    def create_list(body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the list a name.")
        return {"slug": storage.create_list(name)}

    @app.patch("/api/lists/{slug}")
    def rename_list(slug: str, body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the list a name.")
        loaded = get_list(slug)
        if loaded.warning:  # saving would back up the file and write an empty list under the new name
            return error(400, f"Fix or delete this list's file before renaming it. {loaded.warning}")
        storage.rename_list(slug, name)
        return {"slug": slug, "name": name}

    @app.post("/api/lists/{slug}/duplicate")
    def duplicate_list(slug: str, body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the copy a name.")
        loaded = get_list(slug)
        if loaded.warning:  # the copy would silently be empty
            return error(400, f"Can't copy a list whose file is unreadable. {loaded.warning}")
        return {"slug": storage.duplicate_list(slug, name)}

    @app.delete("/api/lists/{slug}")
    def delete_list(slug: str):
        get_list(slug)
        if len(storage.list_slugs()) <= 1:
            return error(400, "You can't delete the last list. Create another one first.")
        storage.delete_list(slug)
        return lists_payload()

    @app.put("/api/active")
    def set_active(body: ActiveChoice):
        get_list(body.slug)
        storage.set_active_list(body.slug)
        return {"active_list": body.slug}

    @app.get("/api/lists/{slug}")
    def read_list(slug: str):
        return list_payload(get_list(slug))

    # -- items ----------------------------------------------------------------------------
    @app.post("/api/lists/{slug}/items")
    def add_item(slug: str, new: NewItem):
        loaded = get_list(slug)
        if not any(i.upc == new.upc for i in loaded.items):
            # A price that came along with the add was fetched just now, so stamp it like a refresh would.
            checked = utc_now() if new.price is not None or new.promo is not None else None
            loaded.items.append(Item(**new.model_dump(), price_checked_at=checked))
            storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug))

    @app.put("/api/lists/{slug}/items")
    def replace_items(slug: str, models: list[ItemModel]):
        loaded = get_list(slug)
        if models:  # an empty body is a no-op, never a clear; removal goes through DELETE
            storage.save_list(slug, loaded.name, [Item(**m.model_dump()) for m in models])
        return list_payload(get_list(slug))

    @app.delete("/api/lists/{slug}/items/{upc}")
    def remove_item(slug: str, upc: str):
        loaded = get_list(slug)
        kept = [i for i in loaded.items if i.upc != upc]
        if len(kept) != len(loaded.items):
            storage.save_list(slug, loaded.name, kept)
        return list_payload(get_list(slug))

    @app.post("/api/lists/{slug}/prices")
    def refresh_prices(slug: str):
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        loaded = get_list(slug)
        if not loaded.items:
            return list_payload(loaded, failed=0)
        succeeded, streak, last_error = 0, 0, None
        now = utc_now()
        for item in loaded.items:
            try:
                fresh = kroger.lookup_product(item.upc, sid)
            except KrogerError as exc:
                last_error = exc
                # Transport failures (status 0) aren't about this item; after a few in a row, stop hammering.
                streak = streak + 1 if exc.status == 0 else 0
                if streak >= TRANSPORT_FAILURE_LIMIT:
                    break
                continue
            succeeded += 1
            streak = 0
            item.price, item.promo, item.price_checked_at = fresh.get("price"), fresh.get("promo"), now
            item.image = fresh.get("image") or item.image
        if not succeeded:
            return error(502, f"Couldn't check prices: {kroger_message(last_error)}")
        storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug), failed=len(loaded.items) - succeeded)

    @app.post("/api/lists/{slug}/push")
    def push(slug: str, models: list[ItemModel]):
        loaded = get_list(slug)
        items = [Item(**m.model_dump()) for m in models]
        if models:
            storage.save_list(slug, loaded.name, items)
        chosen = [i for i in items if i.included and i.quantity > 0]
        if not chosen:
            return error(400, "Nothing is checked to send.")
        try:
            kroger.add_to_cart([(i.upc, i.quantity) for i in chosen])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            # Whole-token match so "1111041" is not flagged when Kroger rejects "0001111041700".
            bad = [i.upc for i in chosen
                   if re.search(rf"(?<!\d){re.escape(i.upc)}(?!\d)", exc.message)]
            return error(502, kroger_message(exc), bad_upcs=bad)
        return {"added": [asdict(i) for i in chosen]}

    # -- import ----------------------------------------------------------------------------
    @app.post("/api/import/parse")
    def import_parse(body: ImportText):
        lines = parse_import(body.text)
        if not lines:
            return error(400, "Couldn't find any items in that text. Try one item per line.")
        return {"lines": [asdict(line) for line in lines]}

    @app.post("/api/import/resolve")
    def import_resolve(body: ResolveRequest):
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        matches, failed, last_error = [], 0, None
        for name in body.names:
            try:
                candidates = kroger.search_products(name, sid)[:RESOLVE_CANDIDATES]
            except KrogerError as exc:
                candidates, failed, last_error = [], failed + 1, exc
            matches.append({"name": name, "candidates": candidates})
        if body.names and failed == len(body.names):
            # One name with no hits is normal; every name failing means Kroger itself is the problem.
            return error(502, f"Couldn't look up any items: {kroger_message(last_error)}")
        return {"matches": matches}

    @app.post("/api/lists/{slug}/import")
    def import_commit(slug: str, picks: list[ImportItem]):
        loaded = get_list(slug)
        by_upc = {i.upc: i for i in loaded.items}
        for pick in picks:
            if pick.upc in by_upc:
                by_upc[pick.upc].quantity += max(1, pick.quantity)
            else:
                item = Item(upc=pick.upc, name=pick.name, size=pick.size, image=pick.image, quantity=max(1, pick.quantity))
                loaded.items.append(item)
                by_upc[pick.upc] = item
        if picks:
            storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug))

    return app
