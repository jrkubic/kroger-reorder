# Meijer catalog (v7.4) — design

**Problem (Jake, 2026-11-17).** Meijer is a favorite store, but Meijer lists (v7) are plain names with
search links. Jake wants Meijer lists to match real products the way Aldi lists do (v7.1): names,
sizes, photos and prices in search and in the import, photo and recipe reviews. The cart stays on
meijer.com for now; Jake chose "catalog now, cart later".

**What the probe found (2026-11-17/18, no browser).**

- `www.meijer.com` is behind Akamai and answers every plain HTTP request, even `robots.txt`, with
  403 "Access Denied". Replaying the site's calls as `aldi.py` does is therefore out, and the app
  will not imitate a browser to get past the block (the same line as Aldi's captcha).
- Meijer's product search is hosted by Constructor.io: `https://ac.cnstrc.com/search/<term>?key=<key>`
  with the public key meijer.com's own pages send (`key_GdYuTcnduTUtsZd6`, seen in Jake's browser in
  the autocomplete request). It answers plain requests with JSON: per result `value` (the full
  name, size after the last comma: "Meijer 2% Reduced Fat Milk, Gallon"), and `data` with `id`
  (digits), `image_url`, `price`, `discountSalePriceValue`, `sale`, `stockLevelStatus`,
  `priceByWeight`/`priceUnit`, `isPurchasable`, `deactivated`. `GET /browse/items?ids=<id>&ids=...`
  returns the same shape for known ids.
- Prices are per store, held as *variations* `<id>_<n>`. `n` is a per-product counter (the gallon of
  2% has 1–54, the quart 1–32, bananas 1–12), not a store or price-zone number, so nothing outside
  meijer.com maps Jake's store (#235, Marysville, 930 Colemans Crossing Blvd) to a variation. Jake's
  browser confirmed the autocomplete request carries no store. The top-level `price` is whichever
  variation surfaces and shifts between calls ($2.78, then $2.94, for the same gallon).
- A `variations_map` parameter aggregates variations in the same request:
  `{"group_by":[{"name":"unit","field":"data.priceUnit"}],"values":{"prices":{"aggregation":"all","field":"data.price"}, ...},"dtype":"array"}`
  returns every store price for each result (up to 50). Their median is stable between calls
  (gallon of 2%: about $2.96, stores $2.08–$3.58; dozen cage-free eggs: about $1.89).
- Product page URLs could not be verified (meijer.com is blocked), so the app does not build them.

## Approaches considered

1. **Constructor.io search with a typical (median) price, shown as "about $X" (chosen).** Real
   products and photos for matching; totals get a sensible figure clearly marked as approximate.
2. **Same catalog without prices.** Never wrong, but loses the estimates Kroger and Aldi lists show.
3. **Exact prices through a browser extension on meijer.com.** Much larger; can come later on top of 1.

## Changes

1. **`kroger_reorder/meijer.py`: `MeijerClient`.** The Aldi client's product surface, so routes and
   the page reuse everything: `search_products(term, limit=10)` (one `/search` request) and
   `lookup_products(upcs)` (one `/browse/items` request; returns `{upc: product}`), both with the
   `variations_map` above. A product is
   `{upc: "meijer:<id>", name, size, brand: "", image, price, promo, in_stock}`:
   - `name`/`size`: split at the last ", " when the tail is short (≤ 24 characters), else the whole
     title and `""`; a by-weight item with no size gets `per lb` from `priceUnit`.
   - `price`: median of the store prices. `promo`: only when more than half the stores have it on
     sale (a store's `discountSalePriceValue` below its own `price`; the two "all" lists share the
     variation order, checked live on 120 products), the median of those stores' sale prices, kept
     only when lower than `price`; lists of different lengths give no promo. Prices of 0 or less are
     ignored. With no variations, the top-level `price` stands in.
   - Results that are not purchasable or are deactivated are dropped. `in_stock` is false only for
     `stockLevelStatus == "outOfStock"`.
   - `transport=` takes an `httpx.MockTransport` in tests. The key is `SEARCH_KEY`, overridable with
     `MEIJER_SEARCH_KEY` in `.env`. The app's server sends no cookie or sign-in and never contacts
     meijer.com: it calls only `ac.cnstrc.com`. Product photos are meijer.com image URLs that the
     user's browser loads (checked: they load in a normal browser; Akamai refuses only headless
     Chrome's user agent).
2. **Errors.** A new `stores.CatalogError(status, message)` with the `unavailable` property becomes
   the base of `AldiError` and the new `MeijerError`; `main.py` handles and catches `CatalogError`
   where it caught `AldiError` (502 handler, import resolve, price refresh). Messages:
   transport → `Couldn't reach Meijer's search: <detail>`; HTTP error →
   `Meijer's search said HTTP <n>.`; a rejected key (HTTP 401/403, or a 400 whose message contains
   `` `key` ``, as Constructor's "You have supplied an invalid `key` ..." does) or an unexpected shape →
   `Meijer's search changed; Meijer lists work as plain lists until the app is updated.` Only that
   message and an unreachable service make the whole catalog unavailable; any other 400 is one bad
   search. A search term is normalised first (whitespace collapsed, cut to 100 characters); one with
   no letter or digit returns no results without a request. Search asks for five more results than
   wanted (at most 100), so dropped unbuyable ones don't shorten the answer.
3. **Providers.** Meijer gets `catalog=True`, `product_prefix="meijer:"`, `cart=False` and a new
   `approx_prices=True`; `product_url` stays unset, so Open at Meijer searches meijer.com for the
   matched product's name. `/api/state` reports `prefix`, `approx_prices` and `site` (the store's
   website host, e.g. `meijer.com`, for the page's wording) per store.
4. **Routes.** `create_app(..., meijer=None)`. `has_catalog`, `catalog_search` and the price
   refresh pick the client by store (`clients = {"aldi": aldi, "meijer": meijer}`) instead of
   assuming Aldi; a store whose client is missing behaves as plain. Lookups for an import run four at
   a time, as for Aldi. Nothing else changes: plain items, hand-off and export work as today.
5. **Page.** `isProduct` uses the store's prefix. On a Meijer list prices read "about $2.96" (sale:
   "about $1.79", old price struck through), and the store line reads "Typical Meijer prices from
   meijer.com's search; your store's may differ." The Aldi store lookup runs only on Aldi lists. A
   product photo that fails to load is replaced by the empty thumbnail placeholder, not a broken image.
6. **Startup.** `__main__` builds a `MeijerClient` (nothing is fetched until a Meijer list searches).

## Data and compatibility

Existing Meijer lists keep their plain items (name keys never contain ":"). Matched items store
`meijer:<id>` in `upc`, so `importer._exact` still only treats all-digit UPCs as Kroger products.
CSV export writes the prefixed id; importing it elsewhere matches by name, as for Aldi.

## Testing

`tests/test_meijer.py` serves trimmed real responses from `tests/fixtures/meijer_*` through
`httpx.MockTransport`: parsing, name/size split, median and promo, dropped results, lookup, every
error message, and that only `ac.cnstrc.com` is called. Route tests in `tests/test_routes.py` use a
`FakeMeijer`: search, add, prices, import resolve and fallback to plain when the client is missing or
down. `tests/test_stores.py` covers the prefix and `is_product("meijer", ...)`. No test touches the
network.

## Docs

README: Features, Using the app (Meijer lists), Configuration (`MEIJER_SEARCH_KEY`),
Troubleshooting (the three messages above), Project structure (`meijer.py`), Design documents,
Version history v7.4. `.env.example` describes `MEIJER_SEARCH_KEY`. AGENTS.md: the layout row and
the "deliberately not done" note (Meijer's catalog is read through its search service; its cart is
still not filled, and the app's server never contacts meijer.com itself).
