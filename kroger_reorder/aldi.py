# kroger_reorder/aldi.py
"""Aldi US catalog: search, product lookups and the store, read from aldi.us.

aldi.us is an Instacart storefront with no public API. This client replays the two GraphQL reads the
site's own search page makes (SearchResultsPlacements, then Items), using the cookies and store context
the search page hands any visitor. The store is the one Instacart picks for the caller's IP address.
Persisted-query hashes come from Instacart's client build and change when it deploys; they are
constants here, overridable from .env, and every failure is an AldiError so Aldi lists can fall back
to plain items. See docs/plans/2026-11-05-aldi-catalog-design.md.

The cart (v7.2) needs the user's own aldi.us sign-in: the session cookie copied from their browser,
because Aldi's login sits behind a captcha. `fill_cart` builds a separate client carrying that cookie
for each send, so the anonymous catalog session never sees it. See
docs/plans/2026-11-06-aldi-cart-design.md.

Product ids are returned as "aldi:<id>" so they can never be mistaken for a Kroger UPC (all digits) or
for a plain item's name key (letters, digits and dashes).
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

SITE = "https://www.aldi.us"
SEARCH_PAGE = "/store/aldi/s"
GRAPHQL = "/graphql"
PRODUCT_PAGE = SITE + "/store/aldi/products/{id}"
PREFIX = "aldi:"
SEARCH_HASH = "aaaf5d56a69b06999a65d6c4e99f5538e5c7d1b55d709c203afb4397cd0c8370"
ITEMS_HASH = "08669bc38e2fa5abb9f4f3cd69451bdecb4fbbe019ba5745e2d5d3edf6c4503f"
LOCATION_HASH = "b80a5ef08c6fe88df0b9a2e78a099dd6141345b213df0f9f517803674b6eee94"
CURRENT_USER_HASH = "91549410d9d88d0829db6c6b3ff323fbc7641ec3a2a53532b1c300b8e08763a2"
ACTIVE_CART_HASH = "6803f97683d706ab6faa3c658a0d6766299dbe1ff55f78b720ca2ef77de7c5c7"
CART_DATA_HASH = "05e3d7448576ff7d464c9244fb6687fabd1bdeee85fdf817e85487003cdb6d44"
UPDATE_CART_HASH = "6dc50d918d98279dc148ef9a359cb5a66695d747253e9a2ec3bef5c86b42f319"
SID_COOKIE = "__Host-instacart_sid"   # the aldi.us sign-in; the only cookie the cart needs
GRID = "SearchContentManagementSearchItemGrid"   # one search result; carousels and ads are other types
IMAGE_SIZE = "296x296"
SEARCH_PAGE_SIZE = 48
SESSION_MAX_AGE = 30 * 60   # seconds; also picks up a new store when the PC's network changes
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
CHANGED = "Aldi's site changed; Aldi lists work as plain lists until the app is updated."
SIGNED_OUT = "Your Aldi sign-in has expired; connect your Aldi cart again."

_JSON_BLOB = re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
_PRICE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)(\.\d{1,2})?")


class AldiError(Exception):
    """aldi.us could not be read. `status` 0 means it was never reached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def unavailable(self) -> bool:
        """The whole catalog is out of reach (no network, or the site changed), not just one search."""
        return self.status == 0 or self.message == CHANGED


@dataclass(frozen=True)
class Context:
    postal_code: str
    shop_id: str
    zone_id: str
    location_id: str


@dataclass
class _Session:
    """One visit to aldi.us: its own cookie jar (an httpx.Client) and the store context it was given.
    A refresh builds a new one, so requests still in flight on the old one are never disturbed."""
    http: httpx.Client
    context: Context
    started: float
    generation: int
    retired: bool = field(default=False)


@dataclass
class CartResult:
    """What a send did: products now in the cart, and products aldi.us would not take."""
    added: list[str]
    rejected: list[str]


def is_product(upc: str) -> bool:
    return upc.startswith(PREFIX) and upc[len(PREFIX):].isdigit()


def product_id(upc: str) -> str:
    return upc[len(PREFIX):]


def _first(pattern: str, text: str) -> str | None:
    m = re.search(rf'"{pattern}":"(\d+)"', text)
    return m.group(1) if m else None


def parse_context(html: str) -> Context:
    """The store context Instacart embeds (URL-encoded JSON) in every storefront page."""
    text = "\n".join(unquote(blob) for blob in _JSON_BLOB.findall(html))
    values = [_first(key, text) for key in ("postalCode", "shopId", "zoneId", "retailerLocationId")]
    if not all(values):
        raise AldiError(502, CHANGED)
    return Context(*values)


def parse_price(text: str | None) -> float | None:
    """A plain dollar amount such as "$2.57" or "$1,299.00". Anything else ("2 for $5", "$1.39/lb",
    "$0.16 each (est.)") is not a per-item price and gives None rather than a wrong number."""
    if not text:
        return None
    m = _PRICE.fullmatch(text.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", "") + (m.group(2) or ""))


class AldiClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, search_hash: str | None = None,
                 items_hash: str | None = None, location_hash: str | None = None, timeout: float = 20.0,
                 clock=time.monotonic) -> None:
        self.hashes = {"SearchResultsPlacements": search_hash or SEARCH_HASH,
                       "Items": items_hash or ITEMS_HASH,
                       "GetRetailerLocationAddress": location_hash or LOCATION_HASH,
                       "LandingCurrentUser": CURRENT_USER_HASH,
                       "ActiveCartId": ACTIVE_CART_HASH,
                       "CartData": CART_DATA_HASH,
                       "UpdateCartItemsMutation": UPDATE_CART_HASH}
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self._session_now: _Session | None = None
        self._generation = 0
        self._lock = threading.Lock()  # one session refresh at a time; each httpx.Client is thread-safe

    @property
    def context(self) -> Context | None:
        return self._session_now.context if self._session_now else None

    # -- plumbing ---------------------------------------------------------------------------
    @staticmethod
    def _send(http: httpx.Client, path: str, params: dict, headers: dict | None = None,
              body: dict | None = None) -> httpx.Response:
        try:
            if body is None:
                response = http.get(path, params=params, headers=headers)
            else:
                response = http.post(path, params=params, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise AldiError(0, f"Couldn't reach aldi.us: {exc.__class__.__name__}") from exc
        if response.status_code == 400:
            # What aldi.us answers for a persisted-query hash it doesn't know (PersistedQueryNotSupported).
            raise AldiError(502, CHANGED)
        if response.status_code >= 400:
            raise AldiError(502, f"Aldi's site said HTTP {response.status_code}.")
        return response

    def _new_client(self) -> httpx.Client:
        return httpx.Client(base_url=SITE, transport=self.transport, timeout=self.timeout, follow_redirects=True,
                            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    def _session(self, stale: _Session | None = None) -> _Session:
        """The current session, starting one when there is none, it is old, or `stale` is still current
        (a request on it just failed). Threads that saw the same failure share one refresh."""
        with self._lock:
            current = self._session_now
            fresh_enough = current is not None and self.clock() - current.started < SESSION_MAX_AGE
            if fresh_enough and (stale is None or stale is not current):
                return current
            http = self._new_client()
            page = self._send(http, SEARCH_PAGE, {"k": "milk"}, {"Accept": "text/html"})
            self._generation += 1
            if current is not None:
                current.retired = True   # left open: other threads may still be reading from it
            self._session_now = _Session(http, parse_context(page.text), self.clock(), self._generation)
            return self._session_now

    def _graphql(self, op: str, variables) -> dict:
        """`variables(context) -> dict`, so a retry after a refresh uses the new session's store."""
        session = self._session()
        try:
            return self._graphql_once(session, op, variables(session.context))
        except AldiError as exc:
            if exc.status == 0:
                raise                       # the network, not the session
            session = self._session(stale=session)   # an expired or rejected session: one fresh start
            return self._graphql_once(session, op, variables(session.context))

    def _graphql_once(self, session: _Session, op: str, variables: dict) -> dict:
        return self._query(session.http, op, variables)

    def _query(self, http: httpx.Client, op: str, variables: dict, mutation: bool = False) -> dict:
        """One persisted GraphQL call: a GET for reads, a POST with a JSON body for a mutation."""
        extensions = {"persistedQuery": {"version": 1, "sha256Hash": self.hashes[op]}}
        headers = {"Accept": "application/json", "Referer": f"{SITE}{SEARCH_PAGE}"}
        if mutation:
            response = self._send(http, GRAPHQL, {"operationName": op}, {**headers, "Origin": SITE},
                                  body={"operationName": op, "variables": variables, "extensions": extensions})
        else:
            params = {"operationName": op, "variables": json.dumps(variables, separators=(",", ":")),
                      "extensions": json.dumps(extensions, separators=(",", ":"))}
            response = self._send(http, GRAPHQL, params, headers)
        try:
            body = response.json()
        except ValueError:
            raise AldiError(502, CHANGED) from None
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise AldiError(502, CHANGED)
        return body["data"]

    @staticmethod
    def _summarize(item: dict) -> dict | None:
        pid = str(item.get("productId") or "")
        if not pid.isdigit():
            return None                     # one odd entry shouldn't sink the whole answer
        section = (item.get("price") or {}).get("viewSection") or {}
        now = parse_price(section.get("priceString"))
        if now is None:
            try:
                now = float(section.get("priceValueString"))
            except (TypeError, ValueError):
                now = None
        regular = parse_price(section.get("fullPriceString"))
        template = ((item.get("viewSection") or {}).get("itemImage") or {}).get("templateUrl")
        image = template.replace("{width=}x{height=}", IMAGE_SIZE) if isinstance(template, str) else None
        on_sale = regular is not None and now is not None and now < regular
        return {
            "upc": PREFIX + pid,
            "name": item.get("name") or "",
            "brand": item.get("brandName") or "",
            "size": item.get("size") or "",
            # On sale the site shows the regular price struck through: same price/promo shape as Kroger.
            "price": regular if on_sale else now,
            "promo": now if on_sale else None,
            "in_stock": bool((item.get("availability") or {}).get("available", True)),
            "image": image,
            "url": PRODUCT_PAGE.format(id=pid),
        }

    def _items(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        data = self._graphql("Items", lambda c: {
            "ids": [f"items_{c.location_id}-{pid}" for pid in ids],
            "shopId": c.shop_id, "zoneId": c.zone_id, "postalCode": c.postal_code})
        items = data.get("items")
        if not isinstance(items, list):
            raise AldiError(502, CHANGED)
        return [p for p in (self._summarize(i) for i in items if isinstance(i, dict)) if p]

    # -- the product surface the routes use (mirrors KrogerClient) --------------------------
    def search_products(self, term: str, limit: int = 10) -> list[dict]:
        data = self._graphql("SearchResultsPlacements", lambda c: {
            "action": None, "query": term, "pageViewId": str(uuid.uuid4()), "elevatedProductId": None,
            "searchSource": "search", "filters": [], "disableReformulation": False, "disableLlm": False,
            "forceInspiration": False, "orderBy": "bestMatch", "clusterId": None, "includeDebugInfo": False,
            "clusteringStrategy": None, "contentManagementSearchParams": {"itemGridColumnCount": 1},
            "shopId": c.shop_id, "postalCode": c.postal_code, "zoneId": c.zone_id, "first": SEARCH_PAGE_SIZE,
        })
        placements = (data.get("searchResultsPlacements") or {}).get("placements")
        if not isinstance(placements, list):
            raise AldiError(502, CHANGED)
        ranked: list[str] = []
        for placement in placements:
            content = (placement or {}).get("content") or {}
            if content.get("__typename") != GRID:
                continue
            for item_id in content.get("itemIds") or []:
                pid = str(item_id).rsplit("-", 1)[-1]
                if pid.isdigit() and pid not in ranked:
                    ranked.append(pid)
        wanted = ranked[:limit]
        by_id = {p["upc"]: p for p in self._items(wanted)}
        return [by_id[PREFIX + pid] for pid in wanted if PREFIX + pid in by_id]

    def lookup_products(self, upcs: list[str]) -> dict[str, dict]:
        """Current details for many "aldi:<id>" products in one request; unknown ids are absent."""
        ids = [product_id(u) for u in dict.fromkeys(upcs) if is_product(u)]
        return {p["upc"]: p for p in self._items(ids)}

    def store(self) -> dict:
        data = self._graphql("GetRetailerLocationAddress", lambda c: {"id": c.location_id})
        view = ((data.get("retailerLocation") or {}).get("viewSection")) or {}
        address = view.get("address") or {}
        lines = [address.get("lineOneString"), address.get("lineTwoString")]
        return {"store_id": self.context.location_id if self.context else "",
                "store_name": view.get("locationDisplayNameString") or "ALDI",
                "address": ", ".join(line for line in lines if line)}

    # -- the signed-in cart -----------------------------------------------------------------
    def _account_client(self, sid: str) -> httpx.Client:
        http = self._new_client()
        http.headers["x-client-identifier"] = "web"
        http.cookies.set(SID_COOKIE, sid, domain="www.aldi.us", path="/")
        return http

    def _who(self, http: httpx.Client) -> dict:
        user = self._query(http, "LandingCurrentUser", {}).get("currentUser")
        if not isinstance(user, dict) or user.get("guest") is not False:
            raise AldiError(401, SIGNED_OUT)
        return {"name": user.get("firstName") or "", "email": user.get("email") or ""}

    def account(self, sid: str) -> dict:
        """Who the cookie signs in as: {"name", "email"}; AldiError(401, SIGNED_OUT) for a guest."""
        with self._account_client(sid) as http:
            return self._who(http)

    def fill_cart(self, sid: str, wanted: list[tuple[str, int]]) -> CartResult:
        """Put "aldi:<id>" products in the signed-in cart. Each ends up with at least the wanted
        quantity: aldi.us sets quantities rather than adding, so a second send never doubles and a
        larger amount already in the cart is kept. Anything else in `wanted` is ignored."""
        want: dict[str, int] = {}
        for upc, quantity in wanted:
            if is_product(upc) and quantity > 0:
                want[upc] = want.get(upc, 0) + quantity
        if not want:
            return CartResult([], [])
        with self._account_client(sid) as http:
            # The signed-in page names the account's own store (its pickup shop may differ from the
            # anonymous catalog session's delivery shop); item ids must use that store's location.
            page = self._send(http, SEARCH_PAGE, {"k": "milk"}, {"Accept": "text/html"})
            context = parse_context(page.text)
            self._who(http)
            basket = self._query(http, "ActiveCartId", {"addressId": None, "shopId": context.shop_id})
            cart_id = (basket.get("shopBasket") or {}).get("cartId")
            if not cart_id:
                raise AldiError(502, CHANGED)
            cart = self._query(http, "CartData", {"id": str(cart_id)}).get("userCart") or {}
            have: dict[str, float] = {}
            for entry in (cart.get("cartItemCollection") or {}).get("cartItems") or []:
                pid = str(((entry or {}).get("basketProduct") or {}).get("productId") or "")
                if pid.isdigit():
                    have[PREFIX + pid] = float(entry.get("quantity") or 0)

            def item_id(upc: str) -> str:
                return f"items_{context.location_id}-{product_id(upc)}"

            def send(batch: list[str]) -> set[str] | None:
                """The products aldi.us took, or None when it refused the whole batch."""
                data = self._query(http, "UpdateCartItemsMutation", {
                    "cartItemUpdates": [{"itemId": item_id(u), "quantity": float(max(have.get(u, 0), want[u])),
                                         "quantityType": "each", "trackingParams": {}} for u in batch],
                    "cartType": "grocery", "requestTimestamp": int(time.time() * 1000), "cartId": str(cart_id),
                }, mutation=True)
                result = data.get("updateCartItems") or {}
                if result.get("errorType") or result.get("__typename") == "CartsCartUpdateError":
                    return None
                if not isinstance(result.get("updatedItemIds"), list):
                    raise AldiError(502, CHANGED)
                took = {str((r or {}).get("itemId")) for r in result["updatedItemIds"]}
                return {u for u in batch if item_id(u) in took}

            products = list(want)
            took = send(products)
            if took is None:
                # One bad product can sink a batch: find it by sending the rest one at a time.
                took = set()
                if len(products) > 1:
                    for upc in products:
                        took |= send([upc]) or set()
            return CartResult(added=[u for u in products if u in took],
                              rejected=[u for u in products if u not in took])
