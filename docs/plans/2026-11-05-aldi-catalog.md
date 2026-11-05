# Aldi catalog (v7.1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Aldi lists get real products (name, size, price, photo) from aldi.us through a small GraphQL replay client, with graceful fallback to plain lists.

**Architecture:** `aldi.py` mirrors `KrogerClient`'s product surface. Providers gain `catalog` / `cart` flags; routes dispatch catalog calls by the list's store. The page branches on the two flags instead of one.

**Tech Stack:** httpx (already a dependency), pytest with `MockTransport`, vanilla JS.

Design: `docs/plans/2026-11-05-aldi-catalog-design.md`.

---

### Task 1: AldiClient

**Files:** Create `kroger_reorder/aldi.py`, `tests/test_aldi.py`, `tests/fixtures/aldi_page.html`, `tests/fixtures/aldi_search.json`, `tests/fixtures/aldi_items.json`, `tests/fixtures/aldi_location.json`.

Tests (MockTransport routes by URL path and `operationName`):
- `test_session_parses_context_from_page`: after one call, `client.context == Context("43040", "48408", "787", "24032")` and the page was fetched once.
- `test_search_returns_ranked_products_with_prices`: ids come from `SearchContentManagementSearchItemGrid` placements in order (carousels ignored); the `Items` call carries `ids=["items_24032-<id>", ...]`; result dicts have `upc`, `name`, `size`, `brand`, `image` (`{width=}x{height=}` → `296x296`), `price`, `promo`, `in_stock`, `url`.
- `test_sale_price_maps_to_promo`: `fullPriceString "$3.99"` + `priceString "$2.99"` → `price 3.99, promo 2.99`; no fullPrice → `price = priceString, promo None`.
- `test_lookup_products_batches_ids`: one `Items` request for three ids; missing id absent from the result.
- `test_store_names_the_location`: `{"store_id": "24032", "store_name": "ALDI - SPR 20 - Marysville", "address": "15740 US-36 E, Marysville, OH 43040"}`.
- Errors: transport → `AldiError(0, "Couldn't reach aldi.us: ...")`; HTTP 500 → `AldiError(500, "Aldi's site said HTTP 500.")`; GraphQL `errors[].message == "PersistedQueryNotFound"` or missing `data.searchResultsPlacements` → `AldiError(502, "Aldi's site changed; Aldi lists work as plain lists until the app is updated.")`; a 403 on graphql re-fetches the page once, then succeeds.
- `test_hash_overrides`: constructor args replace the constants in the request's `extensions`.

Implementation sketch:

```python
SEARCH_PAGE = "https://www.aldi.us/store/aldi/s"
GRAPHQL = "https://www.aldi.us/graphql"
PRODUCT_PAGE = "https://www.aldi.us/store/aldi/products/{id}"
SEARCH_HASH = "aaaf5d56a69b06999a65d6c4e99f5538e5c7d1b55d709c203afb4397cd0c8370"
ITEMS_HASH = "08669bc38e2fa5abb9f4f3cd69451bdecb4fbbe019ba5745e2d5d3edf6c4503f"
LOCATION_HASH = "b80a5ef08c6fe88df0b9a2e78a099dd6141345b213df0f9f517803674b6eee94"
GRID = "SearchContentManagementSearchItemGrid"
IMAGE_SIZE = "296x296"
CHANGED = "Aldi's site changed; Aldi lists work as plain lists until the app is updated."
```

`Context` dataclass; `_parse_context(html)` unquotes every `<script type="application/json">` blob and regexes `"shopId":"(\d+)"`, `"zoneId":"(\d+)"`, `"postalCode":"(\d+)"`, `"retailerLocationId":"(\d+)"`; missing → `AldiError(502, CHANGED)`. `_graphql` sends `operationName`, `variables` (JSON), `extensions` with the hash, headers `Accept: application/json`, `Referer`, browser `User-Agent`; on 401/403 once: drop the session and retry. Search variables as captured (`searchSource "search"`, `orderBy "bestMatch"`, `pageViewId uuid4`, `first 48`, `filters []`, `contentManagementSearchParams {"itemGridColumnCount": 1}`, shop/postal/zone from the context). Commit: `feat(aldi): GraphQL replay client for aldi.us search, items and store`.

### Task 2: Providers with catalog and cart flags

`Provider(key, name, catalog, cart, search_url=None, home_url=None)`; `has_catalog` removed (grep: `main.py`, `app.js`, tests). Kroger `catalog=True, cart=True`; Aldi `catalog=True, cart=False`; Meijer/Walmart `False, False`. `/api/state` → `{"key", "name", "catalog", "cart"}`. `search_link` raises for Kroger only (`not search_url`). Commit: `refactor(stores): catalog and cart flags per provider`.

### Task 3: Routes dispatch by store

`create_app(storage, kroger, aldi=None, ...)`. Helpers: `catalog_client(p)` → `{"kroger": kroger, "aldi": aldi}.get(p.key)` when `p.catalog`; `has_catalog(loaded)` = client is not None. Rules:
- plain items allowed when `not p.cart`; a UPC is required only when `p.cart` (Kroger).
- `GET /api/search?term=&store=` and `POST /api/import/resolve {names, store}`: `provider(store)` → client; Kroger: needs `store_id` ("Pick a store first."); none → 400 `"<Store> lists have no catalog; add items by name."`.
- `POST .../prices`: Kroger path unchanged; Aldi: `aldi.lookup_products([i.upc for i in items if i.upc.isdigit()])`; plain items untouched; `failed` counts numeric upcs not returned.
- `POST .../push`: refused unless `p.cart`.
- `handoff`: `url = PRODUCT_PAGE.format(id=upc)` when `upc.isdigit()` else `search_link`.
- `GET /api/aldi/store` → `aldi.store()`; 400 `"Aldi isn't configured."` when `aldi is None`.
- `AldiError` handler → `error(exc.status or 502, exc.message)`.
- `__main__.py`: `AldiClient()` built from settings (`aldi_search_hash` etc.); `settings.py` reads the three optional overrides.
Tests in `test_routes.py` with `FakeAldi` (search returns two products, lookup by id, store dict, optional `error`). Commit: `feat(routes): Aldi catalog search, matching and prices; dispatch by list store`.

### Task 4: Page

`providerOf(key)` → `{catalog, cart}`; `isHandOff()` → `!cart`; `hasCatalog()` → `catalog`. `search()`: when `hasCatalog()` call `/api/search?store=`; render results as today plus, when `!cart`, a last row `Add “term” as typed` → `POST items {name}`; when `!hasCatalog()` plain add as today. On an Aldi list: show `#store-line` with `Shopping at <name>, prices from aldi.us` (from `/api/aldi/store`, fetched once and cached in `state.aldi_store`; on error show `Aldi: <message>`), show `#price-row`, hide push, show Open at Aldi. `reviewLines`: resolve when `hasCatalog()` with `store`; catch an error whose status is 502 on a non-cart list → plain candidates + warning banner. `renderRow`: "Search for a different item…" when `hasCatalog()`. Item rows show thumb/price when `hasCatalog()`. `pricesStale()` uses `hasCatalog()` instead of `!isHandOff()`. Commit: `feat(page): Aldi lists search the catalog, show prices, fall back to plain rows`.

### Task 5: Docs and version

README: Features ("Aldi catalog"), Using the app (rewrite the Aldi/Meijer/Walmart section: Aldi now matches products; Meijer and Walmart stay plain), Configuration + `.env.example` (`ALDI_SEARCH_HASH`, `ALDI_ITEMS_HASH`, `ALDI_LOCATION_HASH`), Data and privacy (reads from aldi.us, Instacart prices), HTTP API (`store` params, `/api/aldi/store`), Limitations (terms, pricing, store detection by IP, no cart), Troubleshooting (the three Aldi strings + "Aldi isn't configured." + "<Store> lists have no catalog; add items by name."), Design documents, Version history v7.1; AGENTS.md layout row + trap ("aldi.py must never hit the network in tests: pass transport="); `__version__ = "7.1.0"`. Commit: `docs: v7.1 Aldi catalog; version 7.1.0`.

### Task 6: Finish

Full suite, whole-branch review, live smoke test on a scratch data dir (real aldi.us call: search "milk", add, Check prices, Open at Aldi links), merge, push, CI, restart the live server, memory.
