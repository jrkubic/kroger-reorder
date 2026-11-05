# kroger_reorder/aldi.py
"""Aldi US catalog: search, product lookups and the store, read from aldi.us.

aldi.us is an Instacart storefront with no public API. This client replays the two GraphQL reads the
site's own search page makes (SearchResultsPlacements, then Items), using the cookies and store context
the search page hands any visitor. The store is the one Instacart picks for the caller's IP address.
Persisted-query hashes come from Instacart's client build and change when it deploys; they are
constants here, overridable from .env, and every failure is an AldiError so Aldi lists can fall back
to plain items. See docs/plans/2026-11-05-aldi-catalog-design.md.

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
GRID = "SearchContentManagementSearchItemGrid"   # one search result; carousels and ads are other types
IMAGE_SIZE = "296x296"
SEARCH_PAGE_SIZE = 48
SESSION_MAX_AGE = 30 * 60   # seconds; also picks up a new store when the PC's network changes
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
CHANGED = "Aldi's site changed; Aldi lists work as plain lists until the app is updated."

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
                       "GetRetailerLocationAddress": location_hash or LOCATION_HASH}
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self._session_now: _Session | None = None
        self._generation = 0
        self._lock = threading.Lock()  # one session refresh at a time; each httpx.Client is thread-safe

    @property
    def context(self) -> Context | None:
        return self._session_now.context if self._session_now else None

    # -- plumbing ---------------------------------------------------------------------------
    @staticmethod
    def _send(http: httpx.Client, path: str, params: dict, headers: dict | None = None) -> httpx.Response:
        try:
            response = http.get(path, params=params, headers=headers)
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
        params = {"operationName": op,
                  "variables": json.dumps(variables, separators=(",", ":")),
                  "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": self.hashes[op]}},
                                           separators=(",", ":"))}
        response = self._send(session.http, GRAPHQL, params,
                              {"Accept": "application/json", "Referer": f"{SITE}{SEARCH_PAGE}"})
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
