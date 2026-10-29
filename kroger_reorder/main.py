# kroger_reorder/main.py
"""FastAPI app: serves the page and a JSON API over Storage + KrogerClient."""
from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .importer import parse as parse_import
from .kroger import KrogerError, NotSignedIn
from .orders import build_order
from .recipe import extract as extract_recipe, fetch_page, suggest as suggest_recipe
from .settings import DEFAULT_PHOTO_MODEL
from .storage import Item, ListLoad, Storage
from .usage import profile_text, usage_profile
from .vision import VisionError, analyze as analyze_photos

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXPORT_COLUMNS = ("upc", "name", "size", "quantity", "included", "price", "promo", "image")
RESOLVE_CANDIDATES = 3
MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 6 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
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
    size: str | None = None  # Kroger sometimes sends null; stored as ""
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


class Photo(BaseModel):
    media_type: str
    data: str  # base64, no data: prefix


class PhotoRequest(BaseModel):
    photos: list[Photo]
    note: str = ""


class RecipeRequest(BaseModel):
    url: str
    slug: str
    note: str = ""


class ImportItem(BaseModel):
    upc: str
    name: str
    size: str | None = None  # Kroger sometimes sends null; stored as ""
    image: str | None = None
    quantity: int = 1
    # Present when the row came from an exported CSV; a search match leaves them at the defaults.
    included: bool = True
    price: float | None = None
    promo: float | None = None


class ListNotFound(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


class OrderNotFound(Exception):
    def __init__(self, order_id: str) -> None:
        super().__init__(order_id)
        self.order_id = order_id


def create_app(storage: Storage, kroger, *, photo_model: str = DEFAULT_PHOTO_MODEL, photo_timeout: float = 90.0,
               analyzer=analyze_photos, page_fetcher=fetch_page, recipe_suggester=suggest_recipe,
               photo_backend: str | None = None,
               public_url: str = "http://localhost:8000") -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake).
    `analyzer` has the signature of vision.analyze; tests pass a fake so nothing external is called.
    `page_fetcher(url) -> recipe.Page` and `recipe_suggester(...)` are the recipe equivalents of `analyzer`.
    `photo_backend` ('api' | 'cli' | None) and `public_url` are reported to the page via /api/state."""
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

    @app.exception_handler(OrderNotFound)
    async def order_not_found(_: Request, exc: OrderNotFound):
        return error(404, f"No order with id '{exc.order_id}'.")

    @app.exception_handler(VisionError)
    async def vision_error(_: Request, exc: VisionError):
        # Also answers recipe.RecipeError, a VisionError subclass with the same status + message contract.
        return error(exc.status, exc.message)

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

    def current_store() -> dict:
        config = storage.load_config()
        return {"store_id": config.get("store_id"), "store_name": config.get("store_name")}

    def suggestion_line(s) -> dict:
        return {"raw": f"{s.kind}: {s.reason}" if s.reason else s.kind, "name": s.name,
                "quantity": s.quantity, "exact": None, "kind": s.kind, "reason": s.reason}

    def rejected_upcs(exc: KrogerError, upcs: list[str]) -> list[str]:
        # Whole-token match so "1111041" is not flagged when Kroger rejects "0001111041700".
        return [u for u in upcs if re.search(rf"(?<!\d){re.escape(u)}(?!\d)", exc.message)]

    def record_order(items: list[dict], **meta) -> tuple[dict | None, str | None]:
        """Save an order after Kroger accepted the cart. The cart is already filled, so a disk
        failure here is reported as a warning rather than an error."""
        order = build_order(items, store=current_store(), **meta)
        try:
            storage.record_order(order)
        except OSError as exc:
            return None, f"Sent to cart, but couldn't save it to your order history ({exc})."
        return order, None

    # -- page / setup -----------------------------------------------------------------
    @app.middleware("http")
    async def revalidate_page_assets(request: Request, call_next):
        # Browsers cache static files heuristically and keep using an old app.js against a new
        # page after an update, which silently breaks every handler. Make them revalidate (ETag).
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        stamp = int((STATIC_DIR / "app.js").stat().st_mtime)
        return HTMLResponse(html.replace('src="/static/app.js"', f'src="/static/app.js?v={stamp}"'))

    @app.get("/api/state")
    def state():
        config = storage.load_config()
        store = {"store_id": config["store_id"], "store_name": config.get("store_name", "")} if config.get("store_id") else None
        return {"store": store, "signed_in": kroger.is_signed_in(), "photo_backend": photo_backend,
                "public_url": public_url, **lists_payload()}

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
    @app.get("/api/lists/{slug}/export.csv")
    def export_csv(slug: str):
        loaded = get_list(slug)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        for i in loaded.items:
            writer.writerow([i.upc, i.name, i.size, i.quantity, "true" if i.included else "false",
                             "" if i.price is None else i.price, "" if i.promo is None else i.promo, i.image or ""])
        # BOM so Excel opens it as UTF-8; the importer strips it on the way back in.
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'})

    @app.post("/api/lists/{slug}/items")
    def add_item(slug: str, new: NewItem):
        loaded = get_list(slug)
        if not any(i.upc == new.upc for i in loaded.items):
            # A price that came along with the add was fetched just now, so stamp it like a refresh would.
            checked = utc_now() if new.price is not None or new.promo is not None else None
            loaded.items.append(Item(**new.model_dump() | {"size": new.size or ""}, price_checked_at=checked))
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
        fresh_by_upc: dict[str, dict] = {}
        streak, last_error = 0, None
        now = utc_now()
        for item in loaded.items:
            try:
                fresh_by_upc[item.upc] = kroger.lookup_product(item.upc, sid)
            except KrogerError as exc:
                last_error = exc
                # Transport failures (status 0) aren't about this item; after a few in a row, stop hammering.
                streak = streak + 1 if exc.status == 0 else 0
                if streak >= TRANSPORT_FAILURE_LIMIT:
                    break
                continue
            streak = 0
        if not fresh_by_upc:
            return error(502, f"Couldn't check prices: {kroger_message(last_error)}")
        # The lookups are slow and the user may have edited the list meanwhile (unchecked or removed
        # an item). Merge the prices into the list as it is *now* rather than saving our old snapshot.
        current = get_list(slug)
        for item in current.items:
            fresh = fresh_by_upc.get(item.upc)
            if fresh:
                item.price, item.promo, item.price_checked_at = fresh.get("price"), fresh.get("promo"), now
                item.image = fresh.get("image") or item.image
        storage.save_list(slug, current.name, current.items)
        # `failed` is the number of items we set out to check and couldn't, including any skipped after bailing out.
        return list_payload(get_list(slug), failed=len(loaded.items) - len(fresh_by_upc))

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
            return error(502, kroger_message(exc), bad_upcs=rejected_upcs(exc, [i.upc for i in chosen]))
        sent = [asdict(i) for i in chosen]
        order, warning = record_order(sent, list_slug=slug, list_name=loaded.name)
        return {"added": sent, "order": order, "warning": warning}

    # -- orders ------------------------------------------------------------------------
    @app.get("/api/orders")
    def orders():
        return {"orders": storage.list_orders()}

    @app.delete("/api/orders/{order_id}", status_code=204)
    def delete_order(order_id: str):
        if not storage.delete_order(order_id):
            raise OrderNotFound(order_id)
        return Response(status_code=204)

    @app.post("/api/orders/{order_id}/reorder")
    def reorder(order_id: str):
        source = storage.get_order(order_id)
        if source is None:
            raise OrderNotFound(order_id)
        # Tolerate a hand-edited file: skip entries without a UPC or with a non-positive quantity.
        items = [i for i in source.get("items", [])
                 if isinstance(i, dict) and i.get("upc") and int(i.get("quantity", 0) or 0) > 0]
        if not items:
            return error(400, "Nothing to send.")
        try:
            kroger.add_to_cart([(str(i["upc"]), int(i["quantity"])) for i in items])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            return error(502, kroger_message(exc), bad_upcs=rejected_upcs(exc, [str(i["upc"]) for i in items]))
        order, warning = record_order(items, list_slug=source.get("list_slug"),
                                      list_name=source.get("list_name"), reorder_of=order_id)
        return {"order": order, "warning": warning}

    # -- usage profile, photo and recipe suggestions ---------------------------------------------
    @app.get("/api/usage")
    def usage():
        return {"items": usage_profile(storage.list_orders())}

    @app.post("/api/photo/suggest")
    def photo_suggest(body: PhotoRequest):
        if not body.photos:
            return error(400, "Add at least one photo.")
        if len(body.photos) > MAX_PHOTOS:
            return error(413, f"At most {MAX_PHOTOS} photos at a time.")
        decoded = []
        for photo in body.photos:
            ext = PHOTO_TYPES.get(photo.media_type.lower())
            if not ext:
                return error(415, "Photos must be JPEG, PNG, or WebP.")
            try:
                raw = base64.b64decode(photo.data, validate=True)
            except (binascii.Error, ValueError):
                return error(400, "A photo wasn't valid base64.")
            if not raw:
                return error(400, "A photo was empty.")
            if len(raw) > MAX_PHOTO_BYTES:
                return error(413, f"Each photo must be under {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
            decoded.append((ext, raw))
        profile = profile_text(usage_profile(storage.list_orders()))
        # Photos live only for this request, in a private temp dir Claude Code is pointed at.
        with tempfile.TemporaryDirectory(prefix="kroger-photos-") as tmp:
            paths = []
            for n, (ext, raw) in enumerate(decoded, 1):
                path = Path(tmp) / f"photo-{n}{ext}"
                path.write_bytes(raw)
                paths.append(path)
            suggestions = analyzer(paths, body.note, profile, model=photo_model, timeout=photo_timeout)
        lines = [suggestion_line(s) for s in suggestions]
        if not lines:
            return error(422, "Claude couldn't find any groceries in those photos. Try a clearer or closer shot.")
        return {"lines": lines}

    @app.post("/api/recipe/suggest")
    def recipe_suggest(body: RecipeRequest):
        loaded = get_list(body.slug)          # 404 before any network call
        page = page_fetcher(body.url)
        extracted = extract_recipe(page.html)
        profile = profile_text(usage_profile(storage.list_orders()))
        suggestions = recipe_suggester(extracted, body.note, loaded.name, loaded.items, profile,
                                       model=photo_model, timeout=photo_timeout)
        lines = [suggestion_line(s) for s in suggestions]
        if not lines:
            return error(422, "Claude couldn't find any ingredients on that page.")
        return {"title": extracted.title, "lines": lines}

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
                item = Item(upc=pick.upc, name=pick.name, size=pick.size or "", image=pick.image,
                            quantity=max(1, pick.quantity), included=pick.included,
                            price=pick.price, promo=pick.promo)
                loaded.items.append(item)
                by_upc[pick.upc] = item
        if picks:
            storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug))

    return app
