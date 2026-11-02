# Lists for other stores: Aldi, Meijer, Walmart (v7) — design

**Problem (Jake, 2026-10-30).** Everything in the app assumes Kroger: the catalog search, prices,
and Send to cart all go through Kroger's public API. The household also shops at Aldi, Meijer and
Walmart and wants lists for those stores in the same app.

**What the other stores offer.** None of the three has a public API that an individual can use to
fill a cart today. Aldi's online shop runs on Instacart, whose developer platform is closed to new
applications. Meijer has no API at all and blocks automated visitors. Walmart has an affiliate API
(walmart.io) with selective approval and a public "add to cart" link format; Jake has applied for a
key. Scraping any of the three was considered and rejected: it breaks without notice and violates
their terms.

**Decision.** Add **hand-off stores**: a list can belong to Aldi, Meijer or Walmart, holds plain
item names and quantities, and ends not with Send to cart but with an **Open at &lt;store&gt;** panel that
gives a search link per item on the store's own site plus a copyable text list. Kroger lists keep
every current feature unchanged. The store is chosen per list when the list is created (Jake's
choice; Kroger is the default). Walmart catalog matching and the cart link come later, as v7.1,
once the walmart.io key is granted; v7 ships Walmart as a hand-off store so it is useful now.

## Approaches considered

1. **Hand-off providers (chosen).** The app knows each store's search URL and nothing else. Honest
   about what the stores allow, zero fragile dependencies, and the per-list store gives the Walmart
   catalog a place to land later.
2. **Scrapers behind the same provider interface.** Full-featured on day one, broken on week two,
   and against the stores' terms. Rejected.
3. **One store for the whole app** (switch the app between Kroger and Aldi). Simpler data model, but
   Jake keeps lists for several stores at once. Rejected in favour of a store per list.

## Changes

1. **Provider layer `kroger_reorder/stores.py`.** `Provider(key, name, has_catalog, search_url,
   home_url)` and `PROVIDERS` for `kroger` (catalog, no URLs), `aldi`, `meijer`, `walmart`
   (`has_catalog=False`, search URL templates with `{term}`). `DEFAULT_STORE = "kroger"` lives in
   `storage.py` (which must not import the provider table) and is re-exported here.
   `provider(key)` raises `UnknownStore(key)`; `search_link(key, term)` URL-encodes the term;
   `item_key(name)` is the slug of a plain item's name and stands in for its UPC. The page reads
   the provider table from `/api/state` (`stores`).
2. **Storage.** A list file gains `"store"` (missing means Kroger, so existing files need no
   migration). `ListLoad.store`; `save_list(..., store=None)` keeps the current store when none is
   given, so every existing caller is unchanged; `create_list(name, store)`; `duplicate_list` copies
   the store; `list_lists()` rows carry `store`.
3. **Routes.** `POST /api/lists` takes an optional `store` (unknown → 400). List payloads and
   `/api/state` lists carry `store`. On a hand-off list: `POST .../items` accepts `{name, quantity}`
   with no UPC and keys the item by `item_key(name)` (a Kroger list still requires a UPC); `POST
   .../import` does the same with the picked names; `POST .../push` and `POST .../prices` answer 400
   because there is no cart or catalog; new `GET /api/lists/{slug}/handoff` returns the checked
   items with a search link each, the store's home link, and a plain-text version for the clipboard
   (400 on a Kroger list). Photo and recipe suggestions work for every list; the review table on a
   hand-off list skips the catalog lookup.
4. **Importer.** A row is "exact" only when its `upc` is all digits, so a CSV exported from a
   hand-off list (whose `upc` column holds name keys) is re-matched by name wherever it is imported,
   and a Kroger export imported into a hand-off list contributes its names.
5. **Page.** The **+** button and "New list…" in the Photo and Recipe pickers open a small dialog:
   name plus a store dropdown (Kroger preselected). The list picker shows the store after the name
   for non-Kroger lists. On a hand-off list: the search box becomes "Add an item" (plain text, Enter
   or Add); rows have no photo or price; the Kroger store line, Check prices and Send to cart are
   hidden; the footer offers **Open at &lt;store&gt;**, which shows the hand-off panel (one row per checked
   item with quantity and a "Search at &lt;store&gt;" link that opens in a new tab, a **Copy list** button,
   and a link to the store's site). The import review on a hand-off list shows each line with
   Add / Skip and "Use a different name…" in place of the catalog match.
6. **Docs.** README: tagline, Features, Using the app ("Lists for Aldi, Meijer and Walmart"), Data
   and privacy (`store` field, `upc` column meaning), HTTP API, Limitations, Troubleshooting, Design
   documents, Version history; AGENTS.md layout row; version 7.0.0.

## Data flow

Create: page → `POST /api/lists {name, store}` → `storage.create_list` writes
`{"name", "store", "items": []}`. Add on a hand-off list: page → `POST /api/lists/{slug}/items
{name}` → `Item(upc=item_key(name), name, size="")`. Shop: page → `GET /api/lists/{slug}/handoff` →
`{store, store_name, home_url, items: [{name, quantity, url}], text}` → panel; links open the
store's site. Nothing is sent to any store's servers by the app itself.

## Error handling

- Unknown store on create, or in a hand-edited list file: 400 `Unknown store '<key>'. Choose Kroger,
  Aldi, Meijer, or Walmart.`
- Plain item without a name: 400 `Give the item a name.` Kroger item without a UPC: 400 `Search your
  store and pick a product.`
- Push or price check on a hand-off list: 400 `This list is for <Store>; use Open at <Store> instead.`
  Hand-off on a Kroger list: 400 `Kroger lists are sent to the cart instead.`

## Testing

`tests/test_stores.py` (providers, links, keys, unknown store); storage tests for the `store` field,
default, duplicate, and round trip; route tests for every bullet under Changes 3 and 4; the docs
guard covers the new route, module and design doc. No network, no `.env`, no `data/`.

Out of scope: changing a list's store after creation (export, create, import instead); order history
or usage for hand-off trips; Walmart catalog matching and cart links (v7.1); Instacart.
