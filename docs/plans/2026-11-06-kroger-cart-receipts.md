# Kroger cart awareness and receipts (v7.3) Implementation Plan

**Goal:** Warn before a Kroger send doubles what the app already put in the cart, track checkout, and
let a pasted receipt record substitutions, out-of-stock items, refunds and totals.

**Architecture:** Orders carry `status`; `orders.py` gains `cart_contents`, `merge_checkout`,
`apply_receipt`; `usage.py` respects status and outcomes; new `receipt.py` reads a receipt through
`vision.complete`; routes in `main.py`; page panel, overlap dialog and receipt dialog.

Design: `docs/plans/2026-11-06-kroger-cart-receipts-design.md`.

### Task 1: orders + usage
`build_order(..., status=None)`; `is_ordered(order)`; `cart_contents(orders)`;
`merge_checkout(orders, now)`; `apply_receipt(order, parsed, now)`; `usage_profile` skips
non-ordered orders and uses outcomes. Tests first in `tests/test_orders.py`, `tests/test_usage.py`.
Commit `feat(orders): cart status, checkout merge, receipt outcomes`.

### Task 2: storage
`Storage.update_order(order)` and `Storage.replace_orders(remove_ids, add)` under the orders lock.
Commit `feat(storage): update and replace orders`.

### Task 3: receipt reader
`receipt.py`: `SYSTEM`, `user_text(text, items)`, `parse(text) -> dict`, `read(...)`. Tests in
`tests/test_receipt.py`. Commit `feat(receipt): read a pasted Kroger receipt with Claude`.

### Task 4: routes
Overlap 409 and `mode` on push/reorder; `/api/cart`, checkout, empty; `kroger_cart` in state;
`POST /api/orders/{id}/receipt` with an injectable `receipt_reader`; wire in `__main__.py`.
Commit `feat(routes): Kroger cart overlap, checkout and receipts`.

### Task 5: page
Cart panel, overlap dialog, receipt dialog, order badges, "Find it". Commit `feat(page): ...`.

### Task 6: docs, version 7.3.0, review, live check, merge.
