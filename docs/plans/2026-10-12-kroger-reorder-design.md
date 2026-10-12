# Kroger Reorder — Design

Date: 2026-10-12

## Purpose

A small local tool that fills a Kroger cart with a household "standard" grocery list in one click, so re-ordering a pickup order takes seconds instead of searching for each item. Checkout (pickup slot, payment) is finished in the Kroger app.

## Scope decisions

- **Fill the cart, not place the order.** Kroger's official public API has a Cart endpoint that adds items but cannot check out, choose a pickup time, read the cart, or clear it. Browser automation of kroger.com was rejected as brittle and against Kroger's terms.
- **List built by interactive search.** Items are found via Kroger's product catalog scoped to the user's store, then saved locally with their UPC. No hand-hunting for UPCs.
- **Local web page interface** served by a Python + FastAPI process. Chosen over a CLI so a partner can use it too.
- **Single list, single store.** Multiple named lists, order history, and SQLite were considered and deferred (YAGNI).

## Prerequisite

A registered app at developer.kroger.com (free). Provides a client ID and secret and lets the user register `http://localhost:<port>/callback` as the OAuth redirect URL. README documents the steps.

## Known behavior

The Cart API only appends. Pushing into a non-empty cart doubles items. The page reminds the user to start from an empty cart.

## Project layout

```
kroger-reorder/
  app/
    main.py            FastAPI app, routes, serves the page, opens browser on start
    kroger.py          Kroger API client: auth (app token + user OAuth), store lookup,
                       product search, add to cart
    storage.py         Read/write data/list.json, data/tokens.json, data/config.json
    static/index.html  Single page, vanilla JS, no build step
  tests/
  data/                gitignored runtime data
  docs/plans/
  .env.example         KROGER_CLIENT_ID, KROGER_CLIENT_SECRET
  README.md
  pyproject.toml
```

## Page

Three areas:

1. **Setup strip** (hidden once complete): zip code → pick store; "Sign in to Kroger" button.
2. **Add item**: search box → matches from the user's store (name, size, price) → click to add.
3. **Standard list**: one row per item with name, size, quantity field, "include this trip" checkbox, remove button. "Send to cart" button with the empty-cart reminder. On success, shows what was added and a link to kroger.com to check out.

Run with a single command (e.g. `uv run kroger-reorder` or `python -m app`) which starts the server on localhost and opens the browser.

## Data flow

### Auth

- **App token** (client credentials grant, scope `product.compact`): used for store lookup and product search. No user sign-in.
- **User token** (authorization code grant, scope `cart.basic:write`): required for add-to-cart. User signs in once via Kroger's login page; Kroger redirects to localhost with a code; the app exchanges it for access + refresh tokens and stores them.
- Access tokens last ~30 minutes. The client refreshes silently using the refresh token. If refresh is rejected, the sign-in button reappears.

### First-time setup

1. User puts client ID and secret in `.env`. App fails fast with a clear message if missing.
2. User enters zip code; app lists nearby stores; user picks one; store ID saved to config.
3. User clicks "Sign in to Kroger"; OAuth round trip; tokens saved.

### Adding an item

Page → `POST /api/search {term}` → server calls Kroger product search with `filter.locationId` → returns top matches (name, size, price, UPC). Clicking one → `POST /api/items` appends `{upc, name, size, quantity: 1, included: true}` to list.json.

### Sending to cart

Page → `POST /api/push` with the full current list state (quantities, included flags). Server filters to included items and makes one `PUT /v1/cart/add` call with all of them, `modality: "PICKUP"`. Returns the added items for display.

### Stored data (all gitignored, all plain JSON)

- `data/list.json`: `[{upc, name, size, quantity, included}]`
- `data/config.json`: `{store_id, store_name}`
- `data/tokens.json`: `{access_token, refresh_token, expires_at}`

## Error handling

Every failure surfaces as a banner on the page; nothing fails silently.

- Missing/invalid client credentials → server refuses to start, prints what to fix.
- Kroger error on search or add → page shows Kroger's message; list unchanged.
- Add-to-cart rejects a UPC (e.g. discontinued) → Kroger rejects the whole batch; page flags the offending row for replacement.
- Expired/revoked sign-in → sign-in button reappears with a note; list unaffected.
- Corrupt or missing list.json → treated as empty with a warning; not overwritten until the user adds something.

## Testing

The Kroger client is the only network-facing piece; tests mock it.

- **storage**: round-trip list/config/tokens; corrupt-file case.
- **kroger client** (recorded JSON responses): token refresh when expired; product search parsing; cart payload shape.
- **routes** (FastAPI TestClient): add item, update quantity, remove, push with mixed included flags, each error banner above.
- **Manual smoke test** (README): real sign-in, search "milk", push one item, confirm in the Kroger app.
