# Order History Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record every successful "Send to cart" as an order, show the history with dates and summaries, and let any past order be sent to the cart again.

**Architecture:** A pure helper (`app/orders.py`) turns the sent items into an order snapshot with id, timestamp, count, and estimated total. `Storage` gains four methods over a single append-only `data/orders.json`. The list push endpoint records an order after Kroger accepts the cart; three new endpoints list, delete, and reorder. The page gets an **Orders** button that opens a dialog, styled like the existing import dialog.

**Tech Stack:** Python 3.12, FastAPI, pydantic, pytest + Starlette `TestClient`; vanilla JS and HTML in `app/static/`.

**Design:** `docs/plans/2026-10-26-order-history-design.md`. Read it first.

**Safety:** `C:\Projects\kroger-reorder\data\` holds Jake's real lists and live OAuth tokens. Tests use `tmp_path` only. Never write fixtures into `data/`, never delete it. `.env` holds real credentials; never print it.

**Running tests:** from `C:\Projects\kroger-reorder`, `.venv\Scripts\python -m pytest -q`. Every task ends with the whole suite green.

---

### Task 1: `build_order` helper

**Files:**
- Create: `app/orders.py`
- Create: `tests/test_orders.py`

**Step 1: Write the failing tests**

```python
# tests/test_orders.py
from datetime import datetime, timezone

from app.orders import build_order

STORE = {"store_id": "01400943", "store_name": "Kroger Hyde Park"}
NOW = datetime(2026, 10, 26, 13, 14, 0, tzinfo=timezone.utc)


def item(upc, name, **over):
    base = {"upc": upc, "name": name, "size": "1 gal", "image": None, "quantity": 1,
            "included": True, "price": None, "promo": None, "price_checked_at": "2026-10-26T12:00:00+00:00"}
    base.update(over)
    return base


def test_build_order_shape_and_id():
    order = build_order([item("0001", "Milk", price=3.49)], list_slug="standard", list_name="Standard",
                        store=STORE, now=NOW)
    assert order["id"].startswith("20261026T131400Z-")
    assert len(order["id"]) == len("20261026T131400Z-") + 4
    assert order["placed_at"] == "2026-10-26T13:14:00+00:00"
    assert order["list_slug"] == "standard" and order["list_name"] == "Standard"
    assert order["store_id"] == "01400943" and order["store_name"] == "Kroger Hyde Park"
    assert order["reorder_of"] is None
    assert order["item_count"] == 1
    # Snapshot drops list-only fields and keeps what is needed to display and resend.
    assert order["items"] == [{"upc": "0001", "name": "Milk", "size": "1 gal", "image": None,
                               "quantity": 1, "price": 3.49, "promo": None}]


def test_build_order_total_prefers_promo_and_skips_unpriced():
    items = [item("0001", "Milk", quantity=2, price=3.49, promo=2.99),   # 2 x 2.99 = 5.98
             item("0002", "Eggs", quantity=1, price=4.00),               # 4.00
             item("0003", "Bread", quantity=3)]                          # no price, skipped
    order = build_order(items, list_slug="s", list_name="S", store=STORE, now=NOW)
    assert order["estimated_total"] == 9.98
    assert order["item_count"] == 3


def test_build_order_total_is_none_when_nothing_priced():
    order = build_order([item("0003", "Bread")], list_slug="s", list_name="S", store=STORE, now=NOW)
    assert order["estimated_total"] is None


def test_build_order_records_reorder_source_and_tolerates_missing_store():
    order = build_order([item("0001", "Milk")], list_slug="s", list_name="S", store={},
                        reorder_of="20261001T000000Z-abcd", now=NOW)
    assert order["reorder_of"] == "20261001T000000Z-abcd"
    assert order["store_id"] is None and order["store_name"] is None


def test_build_order_ids_differ_for_same_second():
    a = build_order([item("0001", "Milk")], list_slug="s", list_name="S", store=STORE, now=NOW)
    b = build_order([item("0001", "Milk")], list_slug="s", list_name="S", store=STORE, now=NOW)
    assert a["id"] != b["id"]
```

**Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_orders.py -q`
Expected: `ModuleNotFoundError: No module named 'app.orders'`

**Step 3: Implement**

```python
# app/orders.py
"""Order snapshots: what was sent to the Kroger cart, recorded after Kroger accepted it."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone


def _unit_price(item: dict) -> float | None:
    """Promo wins when present; otherwise regular price; None when neither is known."""
    for key in ("promo", "price"):
        if item.get(key) is not None:
            return float(item[key])
    return None


def snapshot_item(item: dict) -> dict:
    """Keep only what is needed to display an order and send it again."""
    return {
        "upc": str(item["upc"]),
        "name": str(item.get("name") or ""),
        "size": str(item.get("size") or ""),
        "image": item.get("image") or None,
        "quantity": int(item.get("quantity", 1)),
        "price": item.get("price"),
        "promo": item.get("promo"),
    }


def build_order(items: list[dict], *, list_slug: str | None, list_name: str | None, store: dict,
                reorder_of: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    snap = [snapshot_item(i) for i in items]
    priced = [(i["quantity"], p) for i in snap if (p := _unit_price(i)) is not None]
    total = round(sum(q * p for q, p in priced), 2) if priced else None
    return {
        "id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}",
        "placed_at": now.isoformat(timespec="seconds"),
        "list_slug": list_slug,
        "list_name": list_name,
        "store_id": store.get("store_id"),
        "store_name": store.get("store_name"),
        "reorder_of": reorder_of,
        "items": snap,
        "estimated_total": total,
        "item_count": len(snap),
    }
```

**Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_orders.py -q`
Expected: `5 passed`

**Step 5: Commit**

```bash
git add app/orders.py tests/test_orders.py
git commit -m "feat(orders): build_order snapshot with estimated total

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Storage for orders

**Files:**
- Modify: `app/storage.py` (add constant near line 16; add methods after `clear_tokens`, line 258)
- Test: `tests/test_storage.py` (append)

**Step 1: Write the failing tests**

Append to `tests/test_storage.py`:

```python
# -- orders -----------------------------------------------------------------

def order(order_id, placed_at):
    return {"id": order_id, "placed_at": placed_at, "list_slug": "standard", "list_name": "Standard",
            "store_id": None, "store_name": None, "reorder_of": None, "items": [], "estimated_total": None,
            "item_count": 0}


def test_orders_start_empty_without_creating_a_file(tmp_path):
    s = Storage(tmp_path)
    assert s.list_orders() == []
    assert not (tmp_path / "orders.json").exists()


def test_record_list_get_delete_round_trip(tmp_path):
    s = Storage(tmp_path)
    s.record_order(order("a", "2026-10-26T13:00:00+00:00"))
    s.record_order(order("b", "2026-10-26T14:00:00+00:00"))
    assert [o["id"] for o in s.list_orders()] == ["b", "a"]
    assert s.get_order("a")["placed_at"] == "2026-10-26T13:00:00+00:00"
    assert s.get_order("zzz") is None
    assert s.delete_order("a") is True
    assert s.delete_order("a") is False
    assert [o["id"] for o in s.list_orders()] == ["b"]
    raw = json.loads((tmp_path / "orders.json").read_text(encoding="utf-8"))
    assert raw == {"orders": [order("b", "2026-10-26T14:00:00+00:00")]}


def test_list_orders_sorts_newest_first_even_if_file_is_out_of_order(tmp_path):
    (tmp_path / "orders.json").write_text(json.dumps({"orders": [
        order("old", "2026-01-01T00:00:00+00:00"), order("new", "2026-10-26T00:00:00+00:00")]}), encoding="utf-8")
    assert [o["id"] for o in Storage(tmp_path).list_orders()] == ["new", "old"]


def test_corrupt_orders_file_is_backed_up_and_treated_as_empty(tmp_path):
    (tmp_path / "orders.json").write_text("{not json", encoding="utf-8")
    s = Storage(tmp_path)
    assert s.list_orders() == []
    assert list(tmp_path.glob("orders.json.corrupt-*"))
    s.record_order(order("a", "2026-10-26T13:00:00+00:00"))
    assert [o["id"] for o in s.list_orders()] == ["a"]


def test_wrong_shape_orders_file_is_backed_up_and_entries_without_id_are_skipped(tmp_path):
    (tmp_path / "orders.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    s = Storage(tmp_path)
    assert s.list_orders() == []
    assert list(tmp_path.glob("orders.json.corrupt-*"))
    (tmp_path / "orders.json").write_text(json.dumps({"orders": [{"placed_at": "x"}, order("ok", "2026-10-26T13:00:00+00:00")]}),
                                          encoding="utf-8")
    assert [o["id"] for o in s.list_orders()] == ["ok"]
```

**Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_storage.py -q -k orders`
Expected: `AttributeError: 'Storage' object has no attribute 'list_orders'` (5 failures)

**Step 3: Implement**

In `app/storage.py`, after `DEFAULT_LIST_NAME = "Standard"` (line 17) add:

```python
ORDERS_FILE = "orders.json"
```

Update the module docstring on line 2 to: `"""Plain-JSON persistence: named grocery lists, order history, store config, and Kroger tokens."""`

After `clear_tokens` (end of class) add:

```python
    # -- order history -------------------------------------------------------
    def _load_orders(self) -> list[dict]:
        """Orders as stored. A corrupt or wrong-shaped file is backed up and treated as empty."""
        with self._lock:
            try:
                raw = self._read(ORDERS_FILE)
            except ValueError:
                self._backup(ORDERS_FILE)
                return []
            if raw is None:
                return []
            if not isinstance(raw, dict) or not isinstance(raw.get("orders"), list):
                self._backup(ORDERS_FILE)
                return []
            return [o for o in raw["orders"] if isinstance(o, dict) and o.get("id")]

    def list_orders(self) -> list[dict]:
        return sorted(self._load_orders(), key=lambda o: str(o.get("placed_at", "")), reverse=True)

    def get_order(self, order_id: str) -> dict | None:
        return next((o for o in self._load_orders() if o["id"] == order_id), None)

    def record_order(self, order: dict) -> None:
        with self._lock:
            self._write(ORDERS_FILE, {"orders": [order, *self._load_orders()]})

    def delete_order(self, order_id: str) -> bool:
        with self._lock:
            orders = self._load_orders()
            kept = [o for o in orders if o["id"] != order_id]
            if len(kept) == len(orders):
                return False
            self._write(ORDERS_FILE, {"orders": kept})
            return True
```

**Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_storage.py -q`
Expected: all pass, including the 5 new tests.

**Step 5: Commit**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat(storage): append-only order history in orders.json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: List push records an order

**Files:**
- Modify: `app/main.py` (imports line 17-19; helpers after `clean_name` ~line 138; `push` at lines 289-307)
- Test: `tests/test_routes.py` (append after the push tests, before `# -- prices`)

**Step 1: Write the failing tests**

Insert after `test_push_kroger_rejection_flags_whole_token_upcs_and_keeps_edits` in `tests/test_routes.py`:

```python
def test_push_records_an_order_with_store_and_total(client, kroger, storage, with_store):
    body = [{**MILK, **FULL, "quantity": 2, "price": 3.49, "promo": 2.99}, {**EGGS, **FULL, "included": False}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 200
    order = r.json()["order"]
    assert r.json()["warning"] is None
    assert order["list_slug"] == "standard" and order["list_name"] == "Standard"
    assert order["store_id"] == "01400943" and order["store_name"] == "Kroger Hyde Park"
    assert order["item_count"] == 1 and order["estimated_total"] == 5.98
    assert [i["upc"] for i in order["items"]] == ["0001"]
    assert [o["id"] for o in storage.list_orders()] == [order["id"]]


def test_rejected_push_records_nothing(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert storage.list_orders() == []


def test_push_still_succeeds_when_history_cannot_be_written(client, storage, monkeypatch):
    def boom(order):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "record_order", boom)
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.status_code == 200
    assert r.json()["order"] is None
    assert "order history" in r.json()["warning"]
```

**Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_routes.py -q -k "records_an_order or records_nothing or history_cannot"`
Expected: `KeyError: 'order'` for the first and third; the second passes already (that is fine, it guards a regression).

**Step 3: Implement**

In `app/main.py` change the imports block to:

```python
from .importer import parse as parse_import
from .kroger import KrogerError, NotSignedIn
from .orders import build_order
from .storage import Item, ListLoad, Storage
```

After `clean_name` (inside `create_app`, in the helpers section) add:

```python
    def current_store() -> dict:
        config = storage.load_config()
        return {"store_id": config.get("store_id"), "store_name": config.get("store_name")}

    def rejected_upcs(exc: KrogerError, upcs: list[str]) -> list[str]:
        # Whole-token match so "1111041" is not flagged when Kroger rejects "0001111041700".
        return [u for u in upcs if re.search(rf"(?<!\d){re.escape(u)}(?!\d)", exc.message)]

    def record_order(items: list[dict], **meta) -> tuple[dict | None, str | None]:
        """Save an order after Kroger accepted the cart. The cart is already filled, so a disk
        failure here is reported as a warning rather than an error."""
        order = build_order(items, store=current_store(), **meta)
        try:
            storage.record_order(order)
        except OSError as exc:
            return None, f"Sent to cart, but couldn't save it to your order history ({exc})."
        return order, None
```

Replace the body of `push` so it reads:

```python
    @app.post("/api/lists/{slug}/push")
    def push(slug: str, models: list[ItemModel]):
        loaded = get_list(slug)
        items = [Item(**m.model_dump()) for m in models]
        if models:
            storage.save_list(slug, loaded.name, items)
        chosen = [i for i in items if i.included and i.quantity > 0]
        if not chosen:
            return error(400, "Nothing is checked to send.")
        try:
            kroger.add_to_cart([(i.upc, i.quantity) for i in chosen])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            return error(502, kroger_message(exc), bad_upcs=rejected_upcs(exc, [i.upc for i in chosen]))
        sent = [asdict(i) for i in chosen]
        order, warning = record_order(sent, list_slug=slug, list_name=loaded.name)
        return {"added": sent, "order": order, "warning": warning}
```

**Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass. `test_push_kroger_rejection_flags_whole_token_upcs_and_keeps_edits` must still pass unchanged (it checks the refactored `rejected_upcs`).

**Step 5: Commit**

```bash
git add app/main.py tests/test_routes.py
git commit -m "feat(api): record an order after a successful cart push

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Order endpoints: list, delete, reorder

**Files:**
- Modify: `app/main.py` (add `OrderNotFound` after `ListNotFound` ~line 81; handler after `list_not_found`; routes after `push`, before `# -- import`)
- Test: `tests/test_routes.py` (append a new section before `# -- prices`)

**Step 1: Write the failing tests**

```python
# -- orders ------------------------------------------------------------------------------

def push_one(client, **over):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL, "price": 3.49, **over}])
    assert r.status_code == 200
    return r.json()["order"]


def test_list_orders_newest_first_with_full_items(client, kroger):
    first = push_one(client)
    second = push_one(client, quantity=3)
    r = client.get("/api/orders")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert [o["id"] for o in orders] == [second["id"], first["id"]]
    assert orders[0]["items"][0]["quantity"] == 3


def test_delete_order(client):
    order = push_one(client)
    assert client.delete(f"/api/orders/{order['id']}").status_code == 204
    assert client.get("/api/orders").json()["orders"] == []
    r = client.delete(f"/api/orders/{order['id']}")
    assert r.status_code == 404
    assert "No order" in r.json()["error"]


def test_reorder_pushes_snapshot_and_records_with_source(client, kroger, with_store):
    source = push_one(client, quantity=2)
    kroger.cart_calls.clear()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    new = r.json()["order"]
    assert new["reorder_of"] == source["id"]
    assert new["list_slug"] == "standard" and new["list_name"] == "Standard"
    assert new["item_count"] == 1 and new["estimated_total"] == 6.98
    assert [o["id"] for o in client.get("/api/orders").json()["orders"]] == [new["id"], source["id"]]


def test_reorder_unknown_is_404(client):
    r = client.post("/api/orders/nope/reorder")
    assert r.status_code == 404
    assert r.json() == {"error": "No order with id 'nope'."}


def test_reorder_with_nothing_sendable_is_400(client, storage):
    storage.record_order({"id": "empty", "placed_at": "2026-10-26T00:00:00+00:00", "items": [{"upc": "0001", "quantity": 0}]})
    r = client.post("/api/orders/empty/reorder")
    assert r.status_code == 400
    assert r.json()["error"] == "Nothing to send."


def test_reorder_kroger_rejection_flags_upcs_and_records_nothing(client, kroger, storage):
    source = push_one(client)
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0001", "bad_upcs": ["0001"]}
    assert [o["id"] for o in storage.list_orders()] == [source["id"]]


def test_reorder_when_not_signed_in_is_401(client, kroger):
    source = push_one(client)
    kroger.cart_error = NotSignedIn()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 401
```

**Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_routes.py -q -k "orders or reorder"`
Expected: 404 / 405 responses where 200, 204, 400, or 502 are expected (7 failures).

**Step 3: Implement**

After `class ListNotFound` add:

```python
class OrderNotFound(Exception):
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
```

After the `list_not_found` handler add:

```python
    @app.exception_handler(OrderNotFound)
    async def order_not_found(_: Request, exc: OrderNotFound):
        return error(404, f"No order with id '{exc.order_id}'.")
```

After `push`, before `# -- import`, add:

```python
    # -- orders ------------------------------------------------------------------------
    @app.get("/api/orders")
    def orders():
        return {"orders": storage.list_orders()}

    @app.delete("/api/orders/{order_id}", status_code=204)
    def delete_order(order_id: str):
        if not storage.delete_order(order_id):
            raise OrderNotFound(order_id)
        return Response(status_code=204)

    @app.post("/api/orders/{order_id}/reorder")
    def reorder(order_id: str):
        source = storage.get_order(order_id)
        if source is None:
            raise OrderNotFound(order_id)
        # Tolerate a hand-edited file: skip entries without a UPC or with a non-positive quantity.
        items = [i for i in source.get("items", [])
                 if isinstance(i, dict) and i.get("upc") and int(i.get("quantity", 0) or 0) > 0]
        if not items:
            return error(400, "Nothing to send.")
        try:
            kroger.add_to_cart([(str(i["upc"]), int(i["quantity"])) for i in items])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            return error(502, kroger_message(exc), bad_upcs=rejected_upcs(exc, [str(i["upc"]) for i in items]))
        order, warning = record_order(items, list_slug=source.get("list_slug"),
                                      list_name=source.get("list_name"), reorder_of=order_id)
        return {"order": order, "warning": warning}
```

Add `Response` to the FastAPI import: `from fastapi import FastAPI, Request, Response`.

**Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add app/main.py tests/test_routes.py
git commit -m "feat(api): list, delete, and reorder past orders

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Orders dialog markup and styles

**Files:**
- Modify: `app/static/index.html` (styles before `.hidden` rule ~line 59; price row at lines 112-115; new dialog before `<script>` at line 152)

No automated test covers the static page beyond `test_index_and_static_js_are_served`. Verify by eye in Task 7.

**Step 1: Add the Orders button**

Replace the price row:

```html
    <div class="row between" style="margin-top:10px">
      <span id="price-status" class="muted"></span>
      <div class="row">
        <button id="orders-btn" class="secondary">Orders</button>
        <button id="check-prices" class="secondary">Check prices</button>
      </div>
    </div>
```

**Step 2: Add the dialog** before `<script src="/static/app.js"></script>`:

```html
<dialog id="orders-dialog">
  <div class="row between">
    <h2 style="margin:0">Orders</h2>
    <button id="orders-close" class="secondary">Close</button>
  </div>
  <p class="muted">Everything sent to your cart from here, newest first. Totals are estimates from shelf prices at the time; Kroger may ring up differently. Tap an order to see its items.</p>
  <div id="orders-banner" class="banner"></div>
  <ul id="orders" class="orders"></ul>
  <p id="orders-empty" class="muted hidden">No orders yet. Your next Send to cart will show up here.</p>
</dialog>
```

**Step 3: Add styles** before the `.hidden` rule:

```css
  .orders > li { display:block; cursor:pointer; }
  .orders > li:hover { background:#f8f8f6; margin:0 -8px; padding:10px 8px; border-radius:8px; }
  .orders .head { display:flex; gap:10px; align-items:center; }
  .orders .tag { font-size:.75rem; background:#eceeea; border-radius:999px; padding:2px 8px; color:var(--muted); margin-left:6px; }
  .orders .details { margin-top:8px; cursor:default; }
  .orders .details li { border-top:1px solid var(--line); }
  .orders .details li:first-child { border-top:0; }
  .orders .qty { color:var(--muted); font-size:.9rem; white-space:nowrap; }
```

**Step 4: Run the suite** (the static-file test still passes):

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add app/static/index.html
git commit -m "feat(ui): orders button and dialog markup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Orders dialog behaviour

**Files:**
- Modify: `app/static/app.js` (state line 6; new section before `// -- push`; push handler lines 399-412)

**Step 1: Add state** next to `importRows`:

```js
let orders = [];        // from GET /api/orders, newest first
let openOrderId = null; // which order row is expanded
```

**Step 2: Add the orders section** before `// -- push ---`:

```js
// -- orders dialog ---------------------------------------------------------------------------

function whenText(iso) {
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function previewText(items) {
  const names = items.slice(0, 3).map(i => esc(i.name));
  const more = items.length - names.length;
  return names.join(', ') + (more > 0 ? ` +${more} more` : '');
}

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

function renderOrders() {
  const ul = $('orders'); ul.innerHTML = '';
  orders.forEach(o => {
    const li = document.createElement('li');
    li.dataset.id = o.id;
    const total = o.estimated_total != null ? ` · est. ${money(o.estimated_total)}` : '';
    const tag = o.reorder_of ? '<span class="tag">reorder</span>' : '';
    const details = o.id !== openOrderId ? '' : `
      <div class="details">
        <ul>${o.items.map(i => `<li>${thumb(i.image)}
          <div class="name"><div>${esc(i.name)}</div><div class="size">${esc(i.size)} ${priceHtml(i)}</div></div>
          <span class="qty">&times;${i.quantity}</span></li>`).join('')}</ul>
        <div class="row" style="margin-top:10px">
          <button data-reorder="${esc(o.id)}">Send to cart again</button>
          <button class="danger" data-delete-order="${esc(o.id)}">Delete</button>
        </div>
      </div>`;
    li.innerHTML = `
      <div class="head"><div class="name">
        <div>${esc(whenText(o.placed_at))} · ${esc(o.list_name || 'List')}${tag}</div>
        <div class="size">${plural(o.item_count, 'item')}${total} · ${previewText(o.items)}</div>
      </div></div>${details}`;
    ul.appendChild(li);
  });
  $('orders-empty').classList.toggle('hidden', orders.length > 0);
}

async function loadOrders() {
  orders = (await api('GET', '/api/orders')).orders;
  renderOrders();
}

$('orders-btn').onclick = async () => {
  banner(null, '', false, $('orders-banner'));
  $('orders-dialog').showModal();
  try { await loadOrders(); }
  catch (e) { banner('error', e.message, false, $('orders-banner')); }
};

$('orders-close').onclick = () => $('orders-dialog').close();

$('orders').addEventListener('click', async (e) => {
  const obanner = $('orders-banner');
  const reorderId = e.target.dataset.reorder;
  const deleteId = e.target.dataset.deleteOrder;
  if (reorderId) {
    e.target.disabled = true;
    banner(null, '', false, obanner);
    const source = orders.find(o => o.id === reorderId);
    try {
      const { order, warning } = await api('POST', `/api/orders/${encodeURIComponent(reorderId)}/reorder`);
      await loadOrders();
      const done = `Sent ${plural(order ? order.item_count : source.item_count, 'item')} to your Kroger cart. ` +
        `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`;
      banner(warning ? 'warn' : 'ok', warning ? `${esc(warning)} ${done}` : done, true, obanner);
    } catch (err) {
      const bad = (err.data && err.data.bad_upcs) || [];
      const names = bad.map(u => source?.items.find(i => i.upc === u)?.name || u);
      banner('error', err.message + (names.length ? ` Rejected: ${names.join(', ')}.` : ''), false, obanner);
      e.target.disabled = false;
    }
    return;
  }
  if (deleteId) {
    if (!confirm('Delete this order from your history?')) return;
    try {
      await api('DELETE', `/api/orders/${encodeURIComponent(deleteId)}`);
      orders = orders.filter(o => o.id !== deleteId);
      if (openOrderId === deleteId) openOrderId = null;
      renderOrders();
    } catch (err) { banner('error', err.message, false, obanner); }
    return;
  }
  if (e.target.closest('.details')) return;  // clicks inside the item list don't collapse it
  const li = e.target.closest('li[data-id]');
  if (!li) return;
  openOrderId = openOrderId === li.dataset.id ? null : li.dataset.id;
  renderOrders();
});
```

Note `api()` parses the body with `.catch(() => ({}))`, so the 204 from DELETE is fine.

**Step 3: Show the history warning after a list push.** Replace the `try` block body inside `$('push').onclick`:

```js
    const { added, warning } = await api('POST', listPath('/push'), cleanItems());
    state.items.forEach(i => delete i.bad);
    const done = `Added to your Kroger cart: ${added.map(i => esc(i.name)).join(', ')}. ` +
      `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`;
    banner(warning ? 'warn' : 'ok', warning ? `${done} ${esc(warning)}` : done, true);
```

**Step 4: Syntax check and suite**

Run: `node --check app/static/app.js` (Node is installed at `C:\Projects\node_modules`'s parent; if `node` is missing, open the page in the browser and check the console instead.)
Expected: no output.

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add app/static/app.js
git commit -m "feat(ui): orders dialog with reorder and delete

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: README and manual check

**Files:**
- Modify: `README.md` (add a section after "## Sale prices")

**Step 1: Document it**

Insert after the "Sale prices" section:

```markdown
## Order history

Every successful **Send to cart** is saved as an order in `data/orders.json` with the date, the list it came from, the items and quantities, and an estimated total from the shelf prices at the time. Click **Orders** next to **Check prices** to see them, newest first. Tap an order to see its items, send that exact order to your cart again, or delete it.

History starts with the first push after this feature was added; Kroger's public API has no way to read past orders. The estimate is from Kroger's catalog prices, not your receipt.
```

**Step 2: Manual check against the real app**

The server running from before this work does not auto-reload. Stop it (the process started this session, or `Ctrl+C` in its window) and start it again:

```powershell
.venv\Scripts\python -m app
```

Then in the browser:
1. **Orders** opens the dialog with the empty-state text.
2. Uncheck everything but one cheap item, **Send to cart**. Green banner. **Orders** now shows one row with today's date, "Standard", "1 item · est. $x.xx", and the item name.
3. Tap the row; items expand. **Send to cart again** adds it to the cart again (confirm in the Kroger app) and a second row tagged "reorder" appears.
4. **Delete** on the reorder row removes it after the confirm.
5. Remove the test items from the Kroger cart in the Kroger app.

Report what actually happened, including anything that did not match.

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: order history

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Done when

- `.venv\Scripts\python -m pytest -q` is green.
- The manual check in Task 7 passed, or every mismatch is listed.
- `git log --oneline -7` shows the seven commits above.
