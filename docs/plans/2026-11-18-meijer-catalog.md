# Meijer catalog (v7.4) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Meijer lists match real Meijer products (name, size, photo, typical price) in search and in
the import, photo and recipe reviews, through Meijer's Constructor.io search service.

**Architecture:** A new `meijer.py` client with the Aldi client's product surface
(`search_products`, `lookup_products`). A shared `stores.CatalogError` lets routes treat Aldi and
Meijer failures alike, and routes pick the catalog client by store instead of assuming Aldi. The page
learns each store's id prefix and whether its prices are approximate.

**Tech Stack:** httpx with `MockTransport`, FastAPI, vanilla JS. No new dependency.

Design: `docs/plans/2026-11-18-meijer-catalog-design.md`.

Run tests with `.venv/Scripts/python -m pytest -q` (Windows) from the checkout. The suite must be
green after every task.

---

### Task 1: `CatalogError`, shared by Aldi and Meijer

**Files:** modify `kroger_reorder/stores.py`, `kroger_reorder/aldi.py`, `kroger_reorder/main.py`;
test `tests/test_stores.py`.

**Step 1: failing test** (`tests/test_stores.py`)

```python
from kroger_reorder.aldi import CHANGED, AldiError
from kroger_reorder.stores import CatalogError


def test_store_errors_share_a_base():
    exc = AldiError(502, CHANGED)
    assert isinstance(exc, CatalogError) and exc.unavailable
    assert CatalogError(0, "Couldn't reach x").unavailable and not CatalogError(502, "HTTP 500").unavailable
```

Run: `pytest tests/test_stores.py -q` → ImportError on `CatalogError`.

**Step 2: implement.** In `stores.py`:

```python
class CatalogError(Exception):
    """A catalog store's site or service failed. `status` 0 means it couldn't be reached."""
    changed_message = ""   # each store's "site changed" message; that and status 0 mean the whole catalog is down

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message

    @property
    def unavailable(self) -> bool:
        return self.status == 0 or (bool(self.changed_message) and self.message == self.changed_message)
```

`AldiError(CatalogError)` keeps its constructor, sets `changed_message = CHANGED`, and drops its own
`unavailable`. (A plain `CatalogError` has no changed message; the test above relies on that.) In
`main.py` import `CatalogError` and replace `AldiError` with `CatalogError` in the 502 exception
handler (`aldi_error` → `catalog_error`), `refresh_catalog_prices`, and `import_resolve` (`down:
list[CatalogError]`, `except CatalogError`). The Aldi cart code keeps catching `AldiError`.

**Step 3:** full suite green. **Commit:** `refactor(stores): CatalogError base for catalog store failures`.

### Task 2: `MeijerClient` and fixtures

**Files:** create `kroger_reorder/meijer.py`, `tests/test_meijer.py`, `tests/fixtures/meijer_search.json`,
`tests/fixtures/meijer_items.json`.

**Step 1: fixtures.** Capture real responses once (this is the only network use in the plan):

```bash
.venv/Scripts/python - <<'EOF'
import httpx, json
from kroger_reorder.meijer import BASE, SEARCH_KEY, _params   # write meijer.py's constants and _params first
s = httpx.get(f"{BASE}/search/milk", params=_params(SEARCH_KEY, num_results_per_page=4)).json()
i = httpx.get(f"{BASE}/browse/items", params=_params(SEARCH_KEY, ids=["4125010210", "4011"])).json()
...
EOF
```

Trim each to `{"response": {"results": [...]}}` with 3–4 results, keep in each result only `value`,
`data` (the fields the client reads: `id`, `image_url`, `price`, `discountSalePriceValue`, `sale`,
`stockLevelStatus`, `priceByWeight`, `priceUnit`, `isPurchasable`, `deactivated`) and
`variations_map` (cut `prices`/`sale_prices` to 5–7 values). Then edit the fixtures so the tests
cover: one product on sale at most stores, one by-weight item (bananas, `priceUnit: "LB"`), one
`isPurchasable: false`, one `stockLevelStatus: "outOfStock"`, one with an empty `variations_map`.

**Step 2: failing tests** (`tests/test_meijer.py`), with a `FakeService` like `test_aldi.FakeSite`
that records `(path, query)` and serves the fixtures or queued overrides:

- `test_search_returns_products_in_order`: `search_products("milk", limit=3)` → `upc` values
  `meijer:<id>` in the fixture's order, `name`/`size` split (`"Meijer 2% Reduced Fat Milk"`,
  `"Gallon"`), `image` from `image_url`, `brand == ""`, and the request went to
  `https://ac.cnstrc.com/search/milk` with `key`, `num_results_per_page=3` and a `variations_map`.
- `test_price_is_the_median_store_price` (odd and even counts) and
  `test_promo_is_the_median_sale_price_when_lower` (equal → `promo is None`).
- `test_without_variations_the_listed_price_stands_in`.
- `test_name_and_size`: parametrize `split_name` — `"Organic Bananas, 5-8 ct"` →
  `("Organic Bananas", "5-8 ct")`; a long tail stays in the name; no comma → `(title, "")`;
  by-weight without size → `size == "per lb"`.
- `test_unbuyable_results_are_dropped` and `test_out_of_stock_is_flagged`.
- `test_lookup_products_batches_ids`: `lookup_products(["meijer:4125010210", "meijer:4011", "4011", "milk"])`
  → one request to `/browse/items` with `ids=4125010210&ids=4011`; result keyed by `upc`; no
  request at all for an input with no Meijer ids.
- `test_errors`: transport error → `MeijerError(0, "Couldn't reach Meijer's search: ConnectError")`;
  500 → `"Meijer's search said HTTP 500."`; 401/403 and a body without `response.results` →
  `CHANGED`; `unavailable` is true for 0 and `CHANGED`.
- `test_only_constructor_is_called`: every recorded request host is `ac.cnstrc.com` and carries no
  `cookie` header.
- `test_key_override`: `MeijerClient(search_key="key_other")` sends `key=key_other`.

Run: `pytest tests/test_meijer.py -q` → ImportError.

**Step 3: implement** `kroger_reorder/meijer.py`:

```python
"""Meijer catalog: search and product lookups through Meijer's search service.

www.meijer.com answers plain HTTP clients with Akamai's "Access Denied", and this app does not imitate a
browser. Meijer's product search is hosted by Constructor.io (ac.cnstrc.com) under the public key the
site's own pages send, and it answers plain requests. Prices there are per store, as variations with
no store number on them, so the price shown is the median across stores, marked approximate on the page.
Never sends a cookie or contacts meijer.com. See docs/plans/2026-11-18-meijer-catalog-design.md.

Product ids are returned as "meijer:<id>", like Aldi's "aldi:<id>"."""
from __future__ import annotations

import json
from statistics import median

import httpx

from .stores import CatalogError

BASE = "https://ac.cnstrc.com"
SEARCH_KEY = "key_GdYuTcnduTUtsZd6"
PREFIX = "meijer:"
MAX_SIZE = 24        # longer tails after the last comma are part of the name
CHANGED = "Meijer's search changed; Meijer lists work as plain lists until the app is updated."
VARIATIONS = json.dumps({"group_by": [{"name": "unit", "field": "data.priceUnit"}], "dtype": "array",
                         "values": {"prices": {"aggregation": "all", "field": "data.price"},
                                    "sale_prices": {"aggregation": "all", "field": "data.discountSalePriceValue"}}})


class MeijerError(CatalogError):
    changed_message = CHANGED


def is_product(upc: str) -> bool:
    return upc.startswith(PREFIX) and upc[len(PREFIX):].isdigit()


def _params(key: str, **extra) -> dict:
    return {"key": key, "variations_map": VARIATIONS, **extra}


def split_name(title: str, by_weight: bool = False, unit: str = "") -> tuple[str, str]:
    head, sep, tail = title.rpartition(", ")
    name, size = (head, tail) if sep and head and len(tail) <= MAX_SIZE else (title, "")
    if not size and by_weight and unit:
        size = f"per {unit.lower()}"
    return name, size


def _prices(result: dict) -> tuple[float | None, float | None]:
    groups = result.get("variations_map") or []
    numbers = lambda key: [float(p) for g in groups if isinstance(g, dict)
                           for p in g.get(key) or [] if isinstance(p, (int, float)) and not isinstance(p, bool)]
    prices, sales = numbers("prices"), numbers("sale_prices")
    if not prices:
        listed = (result.get("data") or {}).get("price")
        return (float(listed) if isinstance(listed, (int, float)) else None), None
    price = round(median(prices), 2)
    promo = round(median(sales), 2) if sales else None
    return price, promo if promo is not None and promo < price else None


class MeijerClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, search_key: str | None = None,
                 timeout: float = 15.0) -> None:
        self.key = search_key or SEARCH_KEY
        self._http = httpx.Client(base_url=BASE, transport=transport, timeout=timeout,
                                  headers={"Accept": "application/json"})

    def _get(self, path: str, **params) -> list[dict]:
        try:
            response = self._http.get(path, params=_params(self.key, **params))
        except httpx.HTTPError as exc:
            raise MeijerError(0, f"Couldn't reach Meijer's search: {exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
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
        data = result.get("data") or {}
        pid = str(data.get("id") or "")
        if not pid.isdigit() or data.get("deactivated") or data.get("isPurchasable") is False:
            return None
        name, size = split_name(str(result.get("value") or ""), bool(data.get("priceByWeight")),
                                str(data.get("priceUnit") or ""))
        price, promo = _prices(result)
        return {"upc": PREFIX + pid, "name": name, "brand": "", "size": size, "price": price, "promo": promo,
                "in_stock": data.get("stockLevelStatus") != "outOfStock", "image": data.get("image_url") or None}

    def search_products(self, term: str, limit: int = 10) -> list[dict]:
        results = self._get(f"/search/{httpx.URL(path=term).raw_path.decode()}", num_results_per_page=limit)
        return [p for p in map(self._summarize, results) if p][:limit]

    def lookup_products(self, upcs: list[str]) -> dict[str, dict]:
        ids = [u[len(PREFIX):] for u in dict.fromkeys(upcs) if is_product(u)]
        if not ids:
            return {}
        products = (self._summarize(r) for r in self._get("/browse/items", ids=ids, num_results_per_page=len(ids)))
        return {p["upc"]: p for p in products if p}
```

(Check the search path encoding: `2% milk` must reach the service as `/search/2%25%20milk`; adjust
the quoting to `urllib.parse.quote(term, safe="")` if the `httpx.URL` form doesn't, and add that case
to `test_search_returns_products_in_order`.)

**Step 4:** `pytest tests/test_meijer.py -q` green, then the full suite.
**Commit:** `feat(meijer): MeijerClient reads Meijer's product search`.

### Task 3: Provider, settings and startup

**Files:** `kroger_reorder/stores.py`, `kroger_reorder/settings.py`, `kroger_reorder/__main__.py`,
`.env.example`; tests `tests/test_stores.py`, `tests/test_settings.py`.

**Step 1: failing tests.**
- `test_stores.py`: update the catalog assertion to `["kroger", "aldi", "meijer"]`; add
  `is_product("meijer", "meijer:4125010210")`, not `"aldi:4125010210"`, not `"4125010210"`, not
  `"whole-milk"`; `shop_link("meijer", "meijer:4125010210", "Meijer 2% Milk")` is the meijer.com
  search for the name; `PROVIDERS["meijer"].approx_prices` and no other store's.
- `test_settings.py`: `MEIJER_SEARCH_KEY=key_x` → `settings.meijer_search_key == "key_x"`; unset → `None`.

**Step 2: implement.** `Provider` gains `approx_prices: bool = False`. Meijer becomes
`Provider("meijer", "Meijer", catalog=True, cart=False, search_url=..., home_url=..., product_prefix="meijer:", approx_prices=True)`.
Update the module docstring and `is_product`'s docstring. `Settings.meijer_search_key: str | None`
read like `aldi_search_hash`. `__main__` builds `MeijerClient(search_key=settings.meijer_search_key)`
and passes `meijer=` to `create_app` (Task 4 adds the parameter; land this line in Task 4 if the
order makes the suite red). `.env.example`:

```
# Optional. Meijer lists search Meijer's products through its search service (Constructor.io) with
# the public key meijer.com's pages use. If Meijer search reports "Meijer's search changed", paste
# the new key here (see README, Troubleshooting).
#MEIJER_SEARCH_KEY=
```

Fix any existing route test that assumed Meijer has no catalog *while a client exists*; tests built
without a Meijer client keep treating Meijer as plain.
**Commit:** `feat(stores): Meijer has a catalog with approximate prices`.

### Task 4: Routes pick the catalog client by store

**Files:** `kroger_reorder/main.py`, `tests/test_routes.py`.

**Step 1: failing tests.** Add `FakeMeijer` (like `FakeAldi`'s catalog half: `search_products`,
`lookup_products`, `error`), fixtures `meijer_client` (`create_app(storage, kroger, aldi=fake_aldi, meijer=fake_meijer)`)
and `meijer_list`. Tests:
- `test_state_reports_meijer_catalog_and_prefixes`: stores carry `prefix` and `approx_prices`;
  Meijer `catalog` is true with a client, false without one.
- `test_search_on_a_meijer_list`: `GET /api/search?term=milk&store=meijer` → the fake's products;
  Aldi's fake is not called.
- `test_add_and_price_refresh_on_a_meijer_list`: add a `meijer:` product and a plain item; `POST
  .../prices` calls `lookup_products` once with only the product and updates its price; the plain
  item is untouched.
- `test_import_resolve_on_meijer` (runs in parallel, returns candidates per name) and
  `test_meijer_down_falls_back`: `MeijerError(0, ...)` → 502 with `"Couldn't look up any items: Couldn't reach Meijer's search: ..."`
  and the search route answers 502 with the message.
- `test_push_on_meijer_is_still_refused` keeps passing (the existing test).

**Step 2: implement.** `create_app(..., aldi=None, meijer=None, ...)`;
`catalog_clients = {"aldi": aldi, "meijer": meijer}`; `has_catalog(p)` →
`p.catalog and (p.key == KROGER or catalog_clients.get(p.key) is not None)`; `catalog_search`
and `refresh_catalog_prices` use `catalog_clients[p.key]` / `catalog_clients[loaded.store]`;
rename `ALDI_PARALLEL` → `CATALOG_PARALLEL` (comment: Aldi and Meijer answer in about a second).
`/api/state` stores add `"prefix": p.product_prefix, "approx_prices": p.approx_prices`; update
`test_state_reports_aldi_catalog_when_configured`'s expected dict. Check every remaining `aldi.`
call outside the cart code and `/api/aldi/*` with `grep -n "aldi\." kroger_reorder/main.py`.
**Commit:** `feat(routes): Meijer lists search, match and price through the Meijer catalog`.

### Task 5: Page

**Files:** `kroger_reorder/static/app.js` (no test harness for the page; verify in the browser in Task 7).

- `isProduct(upc)`: Kroger stays `/^\d+$/`; otherwise `upc.startsWith(store.prefix)` and the rest
  is digits, using the active list's store entry from `state.stores`.
- `priceHtml(p, approx)`: with `approx`, prefix each amount with `about `; pass
  `currentStore().approx_prices` from the list, search and review renderers (orders never are).
- Store line: Aldi keeps its detected-store text; a catalog store with `approx_prices` reads
  "Typical Meijer prices from meijer.com's search; your store's may differ."; `loadAldiStore()`
  runs only when the list's store is `aldi`.
- Update the header comment (three kinds of list) and the import-review comment so they name Meijer.

**Commit:** `feat(page): Meijer lists show catalog matches with approximate prices`.

### Task 6: Docs and version

README: Features (Meijer catalog bullet), Using the app (a Meijer subsection next to Aldi's:
search, review, "about" prices, Open at Meijer, cart still on meijer.com), Configuration
(`MEIJER_SEARCH_KEY`), Troubleshooting rows for the three `MeijerError` messages (verbatim),
Project structure (`meijer.py`), Version history v7.4 row; the Design documents row is already in.
AGENTS.md: layout row for `meijer.py`, stores row wording, a trap note (MeijerClient never hits the
network in tests; key rotation is the usual failure), and the "deliberately not done" note.
`kroger_reorder/__init__.py`: `__version__ = "7.4.0"`.
**Commit:** `docs: v7.4 Meijer catalog; version 7.4.0`.

### Task 7: Finish

Full suite; whole-branch review; live check on a scratch data folder (never `./data`): create a
Meijer list, search "milk", add a match and a plain item, Check prices, import three lines, Open at
Meijer. Then merge, push, watch CI, restart the live server, update memory.
