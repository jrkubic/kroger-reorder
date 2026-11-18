"""Meijer catalog: search and product lookups through Meijer's search service.

www.meijer.com answers plain HTTP clients with Akamai's "Access Denied", and this app does not imitate a
browser. Meijer's product search is hosted by Constructor.io (ac.cnstrc.com) under the public key the
site's own pages send, and it answers plain requests. Prices there are per store, as variations with
no store number on them, so the price shown is the median across stores, marked approximate on the page.
Never sends a cookie or contacts meijer.com. See docs/plans/2026-11-18-meijer-catalog-design.md.

Product ids are returned as "meijer:<id>", like Aldi's "aldi:<id>".
"""
from __future__ import annotations

import json
from http.cookiejar import CookieJar, DefaultCookiePolicy
from statistics import median
from urllib.parse import quote

import httpx

from .stores import CatalogError

BASE = "https://ac.cnstrc.com"
SEARCH_KEY = "key_GdYuTcnduTUtsZd6"   # the public key meijer.com's own pages send
PREFIX = "meijer:"
MAX_SIZE = 24        # longer tails after the last comma are part of the name
LOOKUP_BATCH = 50    # ids per /browse/items request
CHANGED = "Meijer's search changed; Meijer lists work as plain lists until the app is updated."
# Asks for every store's price (and sale price) of each result, grouped by unit, in the same answer.
VARIATIONS = json.dumps({"group_by": [{"name": "unit", "field": "data.priceUnit"}], "dtype": "array",
                         "values": {"prices": {"aggregation": "all", "field": "data.price"},
                                    "sale_prices": {"aggregation": "all", "field": "data.discountSalePriceValue"}}},
                        separators=(",", ":"))


class MeijerError(CatalogError):
    """Meijer's search could not be read. `status` 0 means it was never reached."""
    changed_message = CHANGED


def is_product(upc: str) -> bool:
    return upc.startswith(PREFIX) and upc[len(PREFIX):].isdigit()


def _params(key: str, **extra) -> dict:
    return {"key": key, "variations_map": VARIATIONS, **extra}


def split_name(title: str, by_weight: bool = False, unit: str = "") -> tuple[str, str]:
    """"Meijer 2% Reduced Fat Milk, Gallon" -> ("Meijer 2% Reduced Fat Milk", "Gallon"). A long tail is
    part of the name, not a size. A by-weight item with no size is sold "per lb"."""
    head, sep, tail = title.rpartition(", ")
    name, size = (head, tail) if sep and head and len(tail) <= MAX_SIZE else (title, "")
    if not size and by_weight and unit:
        size = f"per {unit.lower()}"
    return name, size


def _numbers(groups: list, key: str) -> list[float]:
    return [float(p) for g in groups if isinstance(g, dict) for p in g.get(key) or []
            if isinstance(p, (int, float)) and not isinstance(p, bool)]


def _prices(result: dict) -> tuple[float | None, float | None]:
    """(price, promo): the median store price, and the median sale price when it is lower. With no
    variations the listed price (whichever store's surfaced) stands in."""
    groups = result.get("variations_map")
    groups = groups if isinstance(groups, list) else []
    prices, sales = _numbers(groups, "prices"), _numbers(groups, "sale_prices")
    if not prices:
        listed = (result.get("data") or {}).get("price")
        ok = isinstance(listed, (int, float)) and not isinstance(listed, bool)
        return (round(float(listed), 2) if ok else None), None
    price = round(median(prices), 2)
    promo = round(median(sales), 2) if sales else None
    return price, promo if promo is not None and promo < price else None


class MeijerClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, search_key: str | None = None,
                 timeout: float = 15.0) -> None:
        self.key = search_key or SEARCH_KEY
        # A jar that refuses every cookie: whatever the service sets is never sent back.
        jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        self._http = httpx.Client(base_url=BASE, transport=transport, timeout=timeout, cookies=jar,
                                  headers={"Accept": "application/json"})

    def _get(self, path: str, **params) -> list[dict]:
        try:
            response = self._http.get(path, params=_params(self.key, **params))
        except httpx.HTTPError as exc:
            raise MeijerError(0, f"Couldn't reach Meijer's search: {exc.__class__.__name__}") from exc
        if response.status_code in (400, 401, 403):
            raise MeijerError(502, CHANGED)       # a rotated key is a 400 ("invalid `key`"); a bad request too
        if response.status_code >= 400:
            raise MeijerError(502, f"Meijer's search said HTTP {response.status_code}.")
        try:
            results = response.json()["response"]["results"]
        except (ValueError, KeyError, TypeError):
            raise MeijerError(502, CHANGED) from None
        if not isinstance(results, list):
            raise MeijerError(502, CHANGED)
        return [r for r in results if isinstance(r, dict)]

    @staticmethod
    def _summarize(result: dict) -> dict | None:
        data = result.get("data")
        data = data if isinstance(data, dict) else {}
        pid = str(data.get("id") or "")
        if not pid.isdigit() or data.get("deactivated") or data.get("isPurchasable") is False:
            return None                     # one odd or unbuyable entry shouldn't sink the whole answer
        name, size = split_name(str(result.get("value") or ""), bool(data.get("priceByWeight")),
                                str(data.get("priceUnit") or ""))
        price, promo = _prices(result)
        # No "url": meijer.com product pages couldn't be verified, so Open at Meijer searches by name.
        return {"upc": PREFIX + pid, "name": name, "brand": "", "size": size, "price": price, "promo": promo,
                "in_stock": data.get("stockLevelStatus") != "outOfStock", "image": data.get("image_url") or None}

    # -- the product surface the routes use (mirrors KrogerClient and AldiClient) ---------------
    def search_products(self, term: str, limit: int = 10) -> list[dict]:
        results = self._get(f"/search/{quote(term, safe='')}", num_results_per_page=limit)
        return [p for p in map(self._summarize, results) if p][:limit]

    def lookup_products(self, upcs: list[str]) -> dict[str, dict]:
        """Current details for many "meijer:<id>" products, LOOKUP_BATCH per request; unknown ids are absent."""
        ids = [u[len(PREFIX):] for u in dict.fromkeys(upcs) if is_product(u)]
        found: dict[str, dict] = {}
        for start in range(0, len(ids), LOOKUP_BATCH):
            batch = ids[start:start + LOOKUP_BATCH]
            for result in self._get("/browse/items", ids=batch, num_results_per_page=len(batch)):
                product = self._summarize(result)
                if product:
                    found[product["upc"]] = product
        return found
