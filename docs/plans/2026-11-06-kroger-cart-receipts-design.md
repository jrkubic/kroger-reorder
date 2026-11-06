# Kroger cart awareness and receipts (v7.3): design

**Problem (Jake, 2026-11-06).** A Kroger send went into a cart that already held some of the same
products; Kroger's cart adds quantities, so they doubled and he didn't notice at checkout. After the
order he also had substitutions (at order time and at pickup) and a refund for an item he never got,
and the order history still showed what was sent, not what he received.

**What Kroger allows.** The public API can add to a cart and nothing else: no reading the cart, no
removing, no order history, no receipts. Identity gives a profile id only. kroger.com does not answer
a plain HTTP client (requests hang), so the v7.2 pasted-cookie approach doesn't carry over, and the
app will not impersonate a browser. Order details therefore come from what the app sent itself and
from receipts Jake chooses to paste.

## Approaches considered

1. **The app's own record of what it sent (chosen).** Reliable and offline; blind to items added on
   kroger.com or the Kroger app. The page says so.
2. **Paste a receipt (chosen, optional per order).** Covers substitutions, out-of-stock, refunds,
   order number, totals and fuel points at the cost of one paste.
3. **A browser extension on kroger.com.** The only way to see the real cart; unprobed, fragile.
   Deferred.

## Part 1: what's in your cart

- Kroger orders gain `status`: `"in_cart"` when sent, `"ordered"` after **I checked out**,
  `"dropped"` after **I emptied my cart**. An order without `status` (everything before v7.3, and
  Aldi orders) counts as `"ordered"`. Aldi is out of scope: its cart never doubles.
- `orders.cart_contents(orders)`: the in-cart Kroger orders combined per UPC (name, size, image,
  quantity, the dates it was sent).
- **Checkout merges.** **I checked out** turns every in-cart order into one `"ordered"` order (one
  real Kroger order): items summed per UPC, `placed_at` = checkout time, `sent_at` = the send times,
  `merged_from` = the old ids, list names joined. A single in-cart order is just flipped. This keeps
  one history entry per real order, which is what a receipt is matched against.
- **Overlap check on every Kroger send** (push and reorder). If any product being sent is already in
  the cart and the request has no `mode`, the server answers **409** with `overlap`
  (`[{upc, name, in_cart, sending, sent_at}]`) and changes nothing (the list is not saved).
  The page shows the overlap and resends with `?mode=missing` (default button: send only what's
  missing, `max(0, wanted − in cart)` per product; fully covered products come back as
  `already_in_cart`) or `?mode=all` (send everything). If nothing is left to send:
  400 "Everything checked is already in your cart."
- **Usage counts only `"ordered"` orders** (no status counts as ordered); in-cart and dropped sends
  are not purchases.
- Routes: `GET /api/cart` → `{"items": [...], "orders": n}`; `POST /api/cart/checkout` →
  `{"order": merged}` (400 "Your Kroger cart is empty." when nothing is in it);
  `POST /api/cart/empty` → `{"dropped": n}`. `/api/state` adds `kroger_cart` (`{"count": items}`).
- Page: a **Your Kroger cart** panel (in the footer of Kroger lists and on the Orders tab) listing
  the products with quantities and send dates, a note "Only what this app sent; items you added on
  kroger.com or in the Kroger app aren't shown.", and the two buttons. The overlap dialog lists
  each product as "Bananas: 2 in your cart (sent Nov 3), sending 2".

## Part 2: receipts (optional, per order)

- **Add receipt…** on an `"ordered"` Kroger order: paste text (the email or kroger.com order page)
  and/or up to 4 screenshots. `POST /api/orders/{id}/receipt` `{text, photos}`.
- `receipt.read(text, images, order_items)` asks Claude (shared `vision.complete`) for one JSON
  object: `order_number`, `date`, `total`, `savings`, `fuel_points`, and `lines`:
  `{upc (one of the order's, or null), name, status: received|substituted|out_of_stock|refunded,
  substitute (name or null), quantity (received), price (paid, line total)}`. Receipt text is data,
  never instructions.
- `orders.apply_receipt(order, parsed, now)`: header fields that are present overwrite; each line
  whose `upc` is on the order sets that item's `outcome`; lines without a match are kept as
  `receipt.unmatched` names. A later paste (a refund email) updates only what it mentions, so
  pastes merge. `receipt.added_at` = last paste.
- Usage: an item with an outcome counts its received quantity for `received`/`substituted`; it is
  left out for `out_of_stock`/`refunded`.
- Orders tab: order number, receipt total next to the estimate, savings and fuel points, a badge per
  item (Substituted → name, Out of stock, Refunded), unmatched receipt lines listed.
- **Substitutions offer a list change**: "Find it" on a substituted item opens the order's list
  (if it still exists) with the search box filled with the substitute's name, so it can be picked.
- Errors: 400 "This order is still in your cart; check out first." for an in-cart order, 400 for an
  Aldi order, 400 "Paste the receipt or add a screenshot." when empty, VisionError statuses as for
  photos, 422 "Claude couldn't read a receipt in that." when nothing usable came back.

## Testing

`tests/test_orders.py` (cart_contents, merge, apply_receipt), `tests/test_usage.py` (status and
outcomes), `tests/test_receipt.py` (prompt, parsing, bad answers), `tests/test_routes.py` (409
overlap, modes, checkout, empty, receipt route with a fake reader), `tests/test_storage.py`
(update/replace orders).

Out of scope: reading the real Kroger cart, Aldi cart status, loyalty balance, the browser extension.
