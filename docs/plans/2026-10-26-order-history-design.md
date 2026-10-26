# Kroger Reorder v3 — Order history

Date: 2026-10-26. Builds on v2 (`2026-10-12-v2-lists-images-import-design.md`).

## Goal

Show previous orders with dates and summaries, and let any past order be sent to the cart again.

## Constraint

Kroger's public API has no order or cart-read endpoint. "Order" therefore means a successful **Send to cart** from this app. History starts the day this ships and records what was sent, not what was checked out. Backfilling from Kroger confirmation emails was considered and declined by the user.

## Scope decisions

- **Record each successful push** (from a list, or a reorder). A push that Kroger rejects records nothing.
- **Append-only single file** `data/orders.json`, newest first. Chosen over per-order files (no manual editing expected) and over storing history inside list files (lost on list delete, no cross-list view).
- **Actions on an order**: expand to see items, "Send to cart again", "Delete". No edit, no "save as list" (YAGNI; duplicate the source list instead).
- **Estimated total** = Σ quantity × (promo if present else price), skipping items with no price. Shown as "est." because Kroger may ring up differently.

## Data model

```json
{
  "orders": [
    {
      "id": "20261026T131400Z-3f2a",
      "placed_at": "2026-10-26T13:14:00+00:00",
      "list_slug": "standard",
      "list_name": "Standard",
      "store_id": "01600549",
      "store_name": "Kroger Marketplace - Marysville Marketplace",
      "reorder_of": null,
      "items": [
        {"upc": "0001111041700", "name": "Lactaid Whole Milk", "size": "1/2 gal",
         "image": "https://...", "quantity": 1, "price": 4.99, "promo": null}
      ],
      "estimated_total": 71.23,
      "item_count": 20
    }
  ]
}
```

- `id` = UTC timestamp + 4 random hex chars; unique and sortable.
- `reorder_of` holds the source order id when the push came from "Send to cart again"; `list_*` is copied from the source order.
- `list_slug` may point at a list that no longer exists; the UI shows the stored `list_name` regardless.
- Items are a snapshot: no `included` or `price_checked_at`; only sent items are stored.
- Corrupt `orders.json` is renamed `.corrupt-<stamp>` (existing `_backup` helper) and treated as empty, matching list behaviour.

## API

- `GET /api/orders` → `{"orders": [...]}` newest first. Full items included (file is small; one trip per week for years is still tiny).
- `POST /api/orders/{id}/reorder` → pushes the snapshot's `(upc, quantity)` pairs via the existing `add_to_cart`, records a new order with `reorder_of`, returns `{"order": {...}}`. Same 502 + `bad_upcs` handling as list push.
- `DELETE /api/orders/{id}` → 204; 404 if unknown.
- Existing `POST /api/lists/{slug}/push` additionally records an order after a successful `add_to_cart` and returns it as `"order"` alongside `"added"`.

## Storage

`Storage.record_order(order: dict) -> dict`, `Storage.list_orders() -> list[dict]`, `Storage.delete_order(id) -> bool`, all under the existing re-entrant lock, using `_read` / `_write`. Order construction (id, totals, count) lives in a small pure helper in `app/orders.py` so it is testable without storage.

## UI

- New **Orders** button beside **Check prices** opens a `<dialog id="orders-dialog">`, same styling as the import dialog.
- Rows, newest first: local date like "Oct 26, 9:14 AM"; list name (with "reorder" tag when `reorder_of` set); "20 items · est. $71.23"; preview "Lactaid Whole Milk, Eggs, Bananas +17 more".
- Tap a row to expand the items (thumb, name, qty, price). Expanded row has **Send to cart again** and **Delete** buttons. Delete asks for confirmation.
- Reorder success shows the existing green banner; failure shows the Kroger message with offending UPCs, as list push does.
- Empty state: "No orders yet. Your next Send to cart will show up here."

## Error handling

- Push succeeds but recording fails (disk error): the cart is already filled, so return 200 with `"order": null` and a `warning` string; UI shows a yellow banner. Never fail a push after Kroger accepted it.
- Reorder of a deleted order id → 404.
- Reorder with an empty items list (hand-edited file) → 400 "Nothing to send."

## Testing

- `tests/test_orders.py`: build_order computes id shape, count, total (promo preferred, missing prices skipped).
- `tests/test_storage.py`: record/list/delete round-trip, newest-first ordering, corrupt file backed up and treated as empty.
- `tests/test_routes.py`: push records an order; failed push records nothing; reorder calls `add_to_cart` with the snapshot and records with `reorder_of`; reorder 404/400; delete 204/404; recording failure yields 200 + warning.
- Manual: real push from the running app, open Orders, reorder, delete.
