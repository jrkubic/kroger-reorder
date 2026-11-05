# kroger_reorder/aldi.py
"""Aldi US catalog: search, product lookups and the store, read from aldi.us.

aldi.us is an Instacart storefront with no public API. This client replays the two GraphQL reads the
site's own search page makes (SearchResultsPlacements, then Items), using the cookies and store context
the search page hands any visitor. The store is the one Instacart picks for the caller's IP address.
Persisted-query hashes come from Instacart's client build and change when it deploys; they are
constants here, overridable from .env, and every failure is an AldiError so Aldi lists can fall back
to plain items. See docs/plans/2026-11-05-aldi-catalog-design.md.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

SITE = "https://www.aldi.us"
SEARCH_PAGE = "/store/aldi/s"
GRAPHQL = "/graphql"
PRODUCT_PAGE = SITE + "/store/aldi/products/{id}"
SEARCH_HASH = "aaaf5d56a69b06999a65d6c4e99f5538e5c7d1b55d709c203afb4397cd0c8370"
ITEMS_HASH = "08669bc38e2fa5abb9f4f3cd69451bdecb4fbbe019ba5745e2d5d3edf6c4503f"
LOCATION_HASH = "b80a5ef08c6fe88df0b9a2e78a099dd6141345b213df0f9f517803674b6eee94"
GRID = "SearchContentManagementSearchItemGrid"   # one search result; carousels and ads are other types
IMAGE_SIZE = "296x296"
SEARCH_PAGE_SIZE = 48
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
CHANGED = "Aldi's site changed; Aldi lists work as plain lists until the app is updated."

_JSON_BLOB = re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
_PRICE = re.compile(r"\d+(?:\.\d+)?")


class AldiError(Exception):
    """aldi.us could not be read. `status` 0 means it was never reached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Context:
    postal_code: str
    shop_id: str
    zone_id: str
    location_id: str


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


def _price(text: str | None) -> float | None:
    m = _PRICE.search(text or "")
    return float(m.group()) if m else None


class AldiClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, search_hash: str | None = None,
                 items_hash: str | None = None, location_hash: str | None = None, timeout: float = 20.0) -> None:
        self.hashes = {"SearchResultsPlacements": search_hash or SEARCH_HASH,
                       "Items": items_hash or ITEMS_HASH,
                       "GetRetailerLocationAddress": location_hash or LOCATION_HASH}
        self.http = httpx.Client(base_url=SITE, transport=transport, timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        self.context: Context | None = None
        self._lock = threading.Lock()  # one session refresh at a time; httpx.Client itself is thread-safe

    # -- plumbing ---------------------------------------------------------------------------
    def _send(self, path: str, params: dict, headers: dict | None = None) -> httpx.Response:
        try:
            response = self.http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise AldiError(0, f"Couldn't reach aldi.us: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise AldiError(502, f"Aldi's site said HTTP {response.status_code}.")
        return response

    def _session(self, refresh: bool = False) -> Context:
        with self._lock:
            if self.context is None or refresh:
                self.http.cookies.clear()
                page = self._send(SEARCH_PAGE, {"k": "milk"}, {"Accept": "text/html"})
                self.context = parse_context(page.text)
            return self.context

    def _graphql(self, op: str, variables: dict) -> dict:
        params = {"operationName": op,
                  "variables": json.dumps(variables, separators=(",", ":")),
                  "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": self.hashes[op]}},
                                           separators=(",", ":"))}
        headers = {"Accept": "application/json", "Referer": f"{SITE}{SEARCH_PAGE}"}
        try:
            response = self._send(GRAPHQL, params, headers)
        except AldiError as exc:
            if "HTTP 401" not in exc.message and "HTTP 403" not in exc.message:
                raise
            self._session(refresh=True)          # an expired or rejected session: one fresh start
            response = self._send(GRAPHQL, params, headers)
        try:
            body = response.json()
        except ValueError:
            raise AldiError(502, CHANGED) from None
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise AldiError(502, CHANGED)
        return body["data"]

    def _item_ids(self, product_ids: list[str]) -> list[str]:
        return [f"items_{self._session().location_id}-{pid}" for pid in product_ids]

    @staticmethod
    def _summarize(item: dict) -> dict:
        section = (item.get("price") or {}).get("viewSection") or {}
        now, regular = _price(section.get("priceString")), _price(section.get("fullPriceString"))
        template = ((item.get("viewSection") or {}).get("itemImage") or {}).get("templateUrl")
        image = re.sub(r"\{width=\}x\{height=\}", IMAGE_SIZE, template) if template else None
        product_id = str(item["productId"])
        return {
            "upc": product_id,
            "name": item.get("name") or "",
            "brand": item.get("brandName") or "",
            "size": item.get("size") or "",
            # On sale the site shows the regular price struck through: same price/promo shape as Kroger.
            "price": regular if regular is not None else now,
            "promo": now if regular is not None and now is not None and now < regular else None,
            "in_stock": bool((item.get("availability") or {}).get("available", True)),
            "image": image,
            "url": PRODUCT_PAGE.format(id=product_id),
        }

    def _items(self, product_ids: list[str]) -> list[dict]:
        if not product_ids:
            return []
        context = self._session()
        data = self._graphql("Items", {"ids": self._item_ids(product_ids), "shopId": context.shop_id,
                                       "zoneId": context.zone_id, "postalCode": context.postal_code})
        items = data.get("items")
        if not isinstance(items, list):
            raise AldiError(502, CHANGED)
        try:
            return [self._summarize(i) for i in items if isinstance(i, dict)]
        except (KeyError, TypeError) as exc:
            raise AldiError(502, CHANGED) from exc

    # -- the product surface the routes use (mirrors KrogerClient) --------------------------
    def search_products(self, term: str, limit: int = 10) -> list[dict]:
        context = self._session()
        data = self._graphql("SearchResultsPlacements", {
            "action": None, "query": term, "pageViewId": str(uuid.uuid4()), "elevatedProductId": None,
            "searchSource": "search", "filters": [], "disableReformulation": False, "disableLlm": False,
            "forceInspiration": False, "orderBy": "bestMatch", "clusterId": None, "includeDebugInfo": False,
            "clusteringStrategy": None, "contentManagementSearchParams": {"itemGridColumnCount": 1},
            "shopId": context.shop_id, "postalCode": context.postal_code, "zoneId": context.zone_id,
            "first": SEARCH_PAGE_SIZE,
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
                product_id = str(item_id).rsplit("-", 1)[-1]
                if product_id not in ranked:
                    ranked.append(product_id)
        wanted = ranked[:limit]
        by_id = {p["upc"]: p for p in self._items(wanted)}
        return [by_id[pid] for pid in wanted if pid in by_id]

    def lookup_products(self, product_ids: list[str]) -> dict[str, dict]:
        """Current details for many products in one request, keyed by product id; unknown ids are absent."""
        return {p["upc"]: p for p in self._items(list(product_ids))}

    def store(self) -> dict:
        context = self._session()
        data = self._graphql("GetRetailerLocationAddress", {"id": context.location_id})
        view = ((data.get("retailerLocation") or {}).get("viewSection")) or {}
        address = view.get("address") or {}
        lines = [address.get("lineOneString"), address.get("lineTwoString")]
        return {"store_id": context.location_id,
                "store_name": view.get("locationDisplayNameString") or "ALDI",
                "address": ", ".join(line for line in lines if line)}
