# Aldi cart (v7.2): design

**Problem (Jake, 2026-11-05/06).** Aldi lists match real aldi.us products (v7.1) but still end with
Open at Aldi: one product page per item. Jake wants the same one-click fill Kroger lists have, and
offered his aldi.us account to find out whether it is possible.

**What the probe found (2026-11-06).**

- aldi.us adds to a cart with one persisted GraphQL mutation, `UpdateCartItemsMutation` (POST,
  hash `6dc50d91…`), whose body lists `{itemId: "items_<locationId>-<productId>", quantity,
  quantityType: "each"}` updates for a `cartId`. Several items go in one request. The tracking
  parameters the site sends are optional.
- The quantity *replaces* what the cart holds (bananas 1 then 2 gives 2, not 3).
- An unknown product in a batch with good ones is silently left out of `updatedItemIds`; a batch
  whose only item is unknown answers `CartsCartUpdateError` with `cartUpdateErrorDescriptionString`
  ("Sorry, we couldn't add this item because it is no longer available.").
- `ActiveCartId` (`{addressId: null, shopId}`) gives the cart id; delivery and pickup shops share it.
  `CartData` (`{id}`) lists what is in it. `LandingCurrentUser` (`{}`) says who is signed in
  (`guest: false`, first name, email).
- Guests can fill a cart too, but a guest cart lives in the app's cookies, which Jake never sees.
  It has to be his account's cart.
- Sign-in happens at account.aldi.us (Aldi's own Salesforce login, not Instacart), behind Friendly
  Captcha. It never completed in an automated browser, and the app will not try to get past it.
- The one cookie `__Host-instacart_sid`, copied from Jake's signed-in browser, is enough on its own:
  with it the app read his account, his cart, and his pickup store context (shop 93789, location
  24032), added three products, and emptied the cart again. aldi.us did not replace the cookie
  while it was used. How long it lasts is unknown.

## Approaches considered

1. **Paste the session cookie into the app (chosen, Jake 2026-11-06).** One copy from the browser's
   developer tools; the app then fills the cart with plain HTTP like everything else in `aldi.py`.
   Cost: the cookie expires at some point and must be pasted again; the page says so when it does.
2. **A bookmarklet run on aldi.us.** Uses the browser's own session, so no cookie handling, but
   depends on aldi.us letting a page script reach the local app (CSP, private network access) and
   is awkward on phones. Kept as the fallback if cookies turn out to expire within hours.
3. **Automated sign-in.** Blocked by the captcha. Rejected.

## Changes

1. **`aldi.py`.**
   - `AldiClient.account(sid) -> {"name", "email"}`: `LandingCurrentUser` on a session built from the
     cookie; `guest: true` or no user raises `AldiError(401, SIGNED_OUT)`.
   - `AldiClient.fill_cart(sid, wanted: list[(upc, quantity)]) -> CartResult(added, rejected, store)`:
     a fresh httpx client carrying the cookie; the search page for the account's own store context
     (it can differ from the anonymous one: pickup vs delivery shop); `LandingCurrentUser` (signed
     out → `SIGNED_OUT`); `ActiveCartId`; `CartData` for the current quantities; one
     `UpdateCartItemsMutation` setting each product to `max(in cart, wanted)`, so sending twice never
     doubles and never lowers what is already there. Products missing from `updatedItemIds` are
     `rejected`. A `CartsCartUpdateError` on a batch of more than one retries each item alone to find
     the bad ones; on a batch of one it rejects that item. Only `aldi:<id>` products are sent.
   - New constants: `CURRENT_USER_HASH`, `ACTIVE_CART_HASH`, `CART_DATA_HASH`, `UPDATE_CART_HASH`,
     `SIGNED_OUT = "Your Aldi sign-in has expired; connect your Aldi cart again."`. Transport, HTTP and
     changed-site errors are the v7.1 ones. No `.env` overrides for these hashes yet (YAGNI; the
     v7.1 ones exist because reads are used constantly).
2. **Storage.** `aldi_session.json` in the data folder: `{"sid", "name", "email", "connected_at"}`;
   `load_aldi_session()`, `save_aldi_session()`, `clear_aldi_session()`. Never returned by the API
   in full: the page only sees name, email and when.
3. **Providers.** `Provider.linked_cart` (Aldi: True): a cart reached through a pasted sign-in.
   `cart` keeps meaning Kroger's API cart, so plain items, Open at Aldi and everything else about
   Aldi lists stay as they are. `/api/state` reports `linked_cart` and, for Aldi, `aldi_account`
   (`{"name", "connected_at"}` or null).
4. **Routes.**
   - `GET /api/aldi/account` → `{"account": {name, email, connected_at} | null}`.
   - `PUT /api/aldi/account {"sid"}` → validates with `aldi.account()`, saves, returns the account.
     Empty → 400 "Paste the __Host-instacart_sid cookie value." A guest session → 400
     "That cookie isn't signed in to aldi.us. Log in at aldi.us first, then copy it again."
   - `DELETE /api/aldi/account` → forgets it (204).
   - `POST /api/lists/{slug}/push` on an Aldi list: saves the list as today, then sends the checked
     `aldi:` products through `fill_cart`. Plain checked items come back as `skipped` (still shown in
     Open at Aldi). No session → 400 "Connect your Aldi cart first." Nothing sendable → 400 "Nothing
     checked is an Aldi product; use Open at Aldi for items added as typed." `SIGNED_OUT` clears the
     stored session and answers 401 with `aldi_signed_in: false`. Rejected products come back as
     `bad_upcs` like Kroger's.
   - The order is recorded like a Kroger one, with `"cart": "aldi"` and the Aldi store's name, so
     order history and the usage profile include Aldi trips. Reorder dispatches on that field.
5. **Page.** On an Aldi list the footer shows **Send to Aldi cart** next to **Open at Aldi**. Not
   connected, the button reads **Connect Aldi cart…** and opens a dialog: three steps (log in at
   aldi.us, open developer tools → Application/Storage → Cookies → www.aldi.us, copy the value of
   `__Host-instacart_sid`), a password-type field, Connect. Connected, the dialog shows the account
   name and a Disconnect button, reachable from a "Aldi cart: <name> · Change" link in the store
   line. Success banner: "Added to your Aldi cart: … Finish checkout at aldi.us." plus "Not sent
   (no Aldi product): …" for skipped items. Footer note on Aldi lists: "Sending sets each product to
   at least the list's quantity; it never doubles."
6. **Docs.** README (Features, Using the app, Data and privacy: the cookie is a sign-in, stored in
   the data folder, treat it like a password; HTTP API; Limitations: cookie expiry, Instacart
   terms; Troubleshooting rows for every new string; Design documents; Version history v7.2);
   AGENTS.md (the not-done line, the trap about `aldi_session.json`); version 7.2.0.

## Data flow

Send on an Aldi list: page → `POST /api/lists/{slug}/push` (items) → storage saves the list →
`load_aldi_session()` → `aldi.fill_cart(sid, products)` → search page (context) →
`LandingCurrentUser` → `ActiveCartId` → `CartData` → `UpdateCartItemsMutation` → result → order
recorded → page banner. Nothing else is sent to aldi.us; checkout stays on aldi.us.

## Error handling

`AldiError` statuses as in v7.1, plus 401 `SIGNED_OUT`, which also clears the stored session so the
page shows Connect again. A failed send changes nothing in the order history. Cart errors never fall
back silently: the banner says what was not added.

## Testing

`tests/test_aldi.py`: the FakeSite gains `LandingCurrentUser`, `ActiveCartId`, `CartData` and the
mutation (POST), from trimmed real responses with made-up names, emails and ids
(`tests/fixtures/aldi_user.json`, `aldi_cart.json`, `aldi_update.json`, `aldi_update_error.json`):
cookie sent, account context used, max-quantity rule, rejected products, single-item error, batch
retry, guest → `SIGNED_OUT`, non-Aldi ids ignored. `tests/test_storage.py`: session round trip.
`tests/test_routes.py`: the account routes, push on an Aldi list (connected, not connected, plain
items skipped, rejected, signed out clears the session, order recorded with `cart: "aldi"`), reorder
of an Aldi order, `/api/state` never leaks the cookie.

Out of scope: checkout, choosing pickup vs delivery or a time slot, the bookmarklet, Meijer and
Walmart carts.
