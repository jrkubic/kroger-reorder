# kroger_reorder/meijer.py
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
MAX_PAGE = 100       # results per request; also the longest search term sent
SLACK = 5            # extra search results asked for, so dropped (unbuyable) ones don't shorten the answer
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


def _bad_key(response: httpx.Response) -> bool:
    """Constructor answers a rotated or unknown key with HTTP 400 and
    {"message": "You have supplied an invalid `key` ..."}; other 400s (an "invalid filter key", say) are
    about the request."""
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        return False
    return isinstance(message, str) and "`key`" in message.lower()


def _amount(value) -> float | None:
    """A usable price: a positive number. A 0 sale price must never show as "about $0.00"."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    return float(value) if ok else None


def _prices(result: dict) -> tuple[float | None, float | None]:
    """(price, promo): the median store price, and a sale only when more than half the stores have it
    on sale, as the median of their sale prices. The "all" aggregations list `prices` and
    `sale_prices` in the same variation order (checked live, 2026-11-18), so a store's pair shares an
    index; a group whose lists differ in length gives no promo. With no variations the listed price
    (whichever store's surfaced) stands in."""
    groups = result.get("variations_map")
    groups = [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
    unit = (result.get("data") or {}).get("priceUnit")
    same_unit = [g for g in groups if unit and g.get("unit") == unit]
    groups = same_unit or groups          # a product sold in two units: the one its price is quoted in
    prices: list[float] = []
    sales: list[float] = []               # the sale prices of stores where it is on sale
    paired = True
    for group in groups:
        listed, sale = group.get("prices"), group.get("sale_prices")
        listed = listed if isinstance(listed, list) else []
        sale = sale if isinstance(sale, list) else []
        paired = paired and len(listed) == len(sale)
        for n, value in enumerate(listed):
            price = _amount(value)
            if price is None:
                continue
            prices.append(price)
            on_sale = _amount(sale[n]) if n < len(sale) else None
            if on_sale is not None and on_sale < price:
                sales.append(on_sale)
    if not prices:
        listed_price = _amount((result.get("data") or {}).get("price"))
        return (round(listed_price, 2) if listed_price is not None else None), None
    price = round(median(prices), 2)
    if not paired or len(sales) * 2 <= len(prices):
        return price, None
    promo = round(median(sales), 2)
    return price, promo if promo < price else None


class MeijerClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, search_key: str | None = None,
                 timeout: float = 15.0) -> None:
        self.key = search_key or SEARCH_KEY
        # One client for every request, also from the import's worker threads: httpx.Client and its
        # CookieJar are thread-safe. The jar refuses every cookie: whatever the service sets is never sent back.
        jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        self._http = httpx.Client(base_url=BASE, transport=transport, timeout=timeout, cookies=jar,
                                  headers={"Accept": "application/json"})

    def _get(self, path: str, **params) -> list[dict]:
        try:
            response = self._http.get(path, params=_params(self.key, **params))
        except httpx.HTTPError as exc:
            raise MeijerError(0, f"Couldn't reach Meijer's search: {exc.__class__.__name__}") from exc
        if response.status_code in (401, 403) or (response.status_code == 400 and _bad_key(response)):
            raise MeijerError(502, CHANGED)       # the public key was rotated
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
        term = " ".join(term.split())[:MAX_PAGE]
        if not any(c.isalnum() for c in term):
            return []                       # nothing to search for ("", ".", "#?&")
        limit = max(1, min(limit, MAX_PAGE))
        results = self._get(f"/search/{quote(term, safe='')}", num_results_per_page=min(limit + SLACK, MAX_PAGE))
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
