# Aldi catalog (v7.1) — design

**Problem (Jake, 2026-11-05).** Aldi lists (v7) are plain names with search links. Jake wants Aldi lists
to be as accurate as Kroger lists: real products with sizes, prices and photos, matched in the review
table. No public API exists, the Instacart Developer Platform is closed to new applications, the
community scrapers checked are dead, fake, or cover other countries, and Jake declined a paid
scraping service ("I'd build my own before paying a third party"). He accepts that aldi.us is an
Instacart storefront whose terms forbid automation: this is a personal, non-commercial tool, and
Aldi lists must keep working as plain lists whenever the site changes.

**What the probe found (2026-11-05, no browser).** `GET https://www.aldi.us/store/aldi/s?k=<term>`
answers 200 with session cookies and an embedded JSON context chosen by the caller's IP: postal
code, `shopId`, `zoneId`, `retailerLocationId` (Jake's: 43040 / 48408 / 787 / 24032, "ALDI - SPR 20 -
Marysville"). With those cookies, two persisted GraphQL queries answer in about a second:
`SearchResultsPlacements` (ranked product ids as one `SearchContentManagementSearchItemGrid`
placement per result) and `Items` (name, size, brand, price, regular price when on sale, stock level,
image template, product slug) for a batch of `items_<locationId>-<productId>` ids. A third,
`GetRetailerLocationAddress`, names the store. Product pages open at
`https://www.aldi.us/store/aldi/products/<productId>`. The query hashes live in Instacart's client
build, not in the page, so they cannot be rediscovered automatically; they are constants here with
`.env` overrides.

## Approaches considered

1. **Replay the storefront's own GraphQL calls (chosen).** Two GETs, no browser, sub-second, same
   product shape as Kroger's client. Fragile at Instacart's deploy cadence; mitigated by detection,
   overrides and the plain-list fallback.
2. **Headless browser on the host.** Survives hash changes but needs Chromium on the PC and in
   Docker, takes seconds per search, and is just as much automation. Rejected.
3. **Paid scraping actor.** Rejected by Jake.

## Changes

1. **`kroger_reorder/aldi.py`: `AldiClient`.** Same product surface as `KrogerClient` so routes and
   the page reuse everything: `search_products(term, limit=10)`, `lookup_products(ids)` (one batched
   `Items` call), `store()` (id, name, address). Internals: `_session()` fetches the search page once,
   keeps cookies and the parsed `Context`, and refreshes on the first failure; `_graphql(op, variables,
   hash)`; a product dict `{upc: productId, name, size, brand, image, price, promo, in_stock, url}` where
   `price` is the regular price and `promo` the current price when the two differ. Errors are
   `AldiError(status, message)`: transport → `Couldn't reach aldi.us: ...`; HTTP error → `Aldi's site
   said HTTP <n>.`; persisted-query-not-found or an unexpected shape → `Aldi's site changed; Aldi
   lists work as plain lists until the app is updated.` The hashes (`SEARCH_HASH`, `ITEMS_HASH`,
   `LOCATION_HASH`) are module constants, overridable by `ALDI_SEARCH_HASH`, `ALDI_ITEMS_HASH`,
   `ALDI_LOCATION_HASH` in `.env`, so a hash change is a `.env` edit, not a release.
2. **Providers.** `Provider` becomes `(key, name, catalog, cart, search_url, home_url)`: Kroger has
   catalog and cart; Aldi has a catalog and no cart; Meijer and Walmart have neither. Plain items
   are allowed on any list without a cart. `/api/state` reports `catalog` and `cart` per store.
3. **Routes.** `create_app(storage, kroger, aldi=None, ...)`; `catalog_client(provider)` returns the
   client for a catalog store or `None` (Aldi not configured → behaves as plain). `GET /api/search`
   and `POST /api/import/resolve` take an optional `store` (default `kroger`) and dispatch; Kroger
   still needs a chosen store, Aldi does not. `POST .../prices` dispatches too (Aldi: one batched
   lookup). `POST .../items` on an Aldi list accepts either a product (`upc`) or a plain `name`.
   `GET /api/lists/{slug}/handoff` links an item with a numeric `upc` to its product page, a plain one
   to a search. New `GET /api/aldi/store` returns the detected store for the page header. Push stays
   Kroger-only. An `AldiError` becomes a 502 with its message.
4. **Page.** Two flags per store, `catalog` and `cart`. On an Aldi list: the search box searches the
   catalog like Kroger, with rows showing photo, size and price, and a final row "Add “<term>” as
   typed" for things the catalog lacks; Check prices and price badges work; the store line reads
   "Shopping at ALDI - SPR 20 - Marysville, prices from aldi.us"; Send to cart stays hidden and
   Open at Aldi links each matched item to its product page. The import review resolves against
   Aldi; when the resolve call fails with an Aldi error the rows fall back to plain add/skip with the
   error shown as a warning, so Photo, Recipe and Import never dead-end.
5. **Docs.** README (Features, Using the app, Configuration for the three overrides, Data and
   privacy, HTTP API, Limitations incl. the Instacart-pricing and terms notes, Troubleshooting,
   Design documents, Version history); AGENTS.md; `.env.example`; version 7.1.0.

## Data flow

Search on an Aldi list: page → `GET /api/search?term=milk&store=aldi` → `AldiClient.search_products`
→ (session: page fetch once) → `SearchResultsPlacements` → first 10 grid item ids →
`Items` → product dicts → page. Prices: `POST .../prices` → `lookup_products([upc...])` → merged
as for Kroger. Nothing is sent to Aldi or Instacart beyond those reads; no cart, no login.

## Error handling

Every `AldiError` is a 502 whose message is one of the three strings above; the page shows it and,
in the review table, falls back to plain rows. A missing session or a 401/403 triggers one page
re-fetch before giving up. Kroger behaviour is unchanged.

## Testing

`tests/test_aldi.py` with `httpx.MockTransport` and trimmed real responses under `tests/fixtures/`
(page context, search placements, items, location): context parsing, ranking, price/promo mapping,
image template, batching, each error path, hash overrides. Route tests with a `FakeAldi`. The docs
guard covers the new module, route, settings and design doc.

Out of scope: an Instacart login and cart filling (Jake offered to create an account; v7.2 if
feasible), choosing a different Aldi store than the IP-detected one, Meijer and Walmart catalogs.
Prices are Instacart's, which can be higher than shelf prices; the README says so.
