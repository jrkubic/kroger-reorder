# tests/test_orders.py
from datetime import datetime, timezone

from kroger_reorder.orders import build_order

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


# -- v7.3: cart status, checkout merge, receipts ---------------------------------------------
from kroger_reorder.orders import apply_receipt, cart_contents, is_ordered, merge_checkout  # noqa: E402


def sent(order_id, placed_at, *items, status="in_cart", cart="kroger", list_name="Weekly"):
    return {"id": order_id, "placed_at": placed_at, "list_slug": "weekly", "list_name": list_name,
            "store_id": "01400943", "store_name": "Kroger Hyde Park", "reorder_of": None, "cart": cart,
            "status": status,
            "items": [{"upc": u, "name": n, "size": "", "image": None, "quantity": q, "price": p, "promo": None}
                      for u, n, q, p in items]}


def test_build_order_status_is_kept_when_given():
    assert build_order([item("1", "Milk")], list_slug=None, list_name=None, store=STORE, now=NOW,
                       status="in_cart")["status"] == "in_cart"
    assert "status" not in build_order([item("1", "Milk")], list_slug=None, list_name=None, store=STORE, now=NOW)


def test_is_ordered():
    assert is_ordered({"id": "x"})                       # before v7.3
    assert is_ordered({"id": "x", "status": "ordered"})
    assert not is_ordered({"id": "x", "status": "in_cart"})
    assert not is_ordered({"id": "x", "status": "dropped"})


def test_cart_contents_combines_in_cart_kroger_orders():
    orders = [
        sent("b", "2026-11-03T10:00:00+00:00", ("1", "Bananas", 2, 0.5), ("2", "Milk", 1, 3.0)),
        sent("a", "2026-11-01T10:00:00+00:00", ("1", "Bananas", 1, 0.5)),
        sent("c", "2026-10-01T10:00:00+00:00", ("3", "Bread", 1, 2.0), status="ordered"),
        sent("d", "2026-11-04T10:00:00+00:00", ("4", "Eggs", 1, 2.0), cart="aldi"),
        sent("e", "2026-11-04T10:00:00+00:00", ("5", "Jam", 1, 2.0), status="dropped"),
    ]
    rows = {r["upc"]: r for r in cart_contents(orders)}
    assert set(rows) == {"1", "2"}
    assert rows["1"]["quantity"] == 3 and rows["1"]["name"] == "Bananas"
    assert rows["1"]["sent_at"] == ["2026-11-01T10:00:00+00:00", "2026-11-03T10:00:00+00:00"]
    assert rows["2"]["quantity"] == 1


def test_merge_checkout_combines_sends_into_one_order():
    now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    orders = [
        sent("b", "2026-11-03T10:00:00+00:00", ("1", "Bananas", 2, 0.5), list_name="Extras"),
        sent("a", "2026-11-01T10:00:00+00:00", ("1", "Bananas", 1, 0.5), ("2", "Milk", 1, 3.0)),
        sent("c", "2026-10-01T10:00:00+00:00", ("3", "Bread", 1, 2.0), status="ordered"),
    ]
    merged, removed = merge_checkout(orders, now=now)
    assert removed == ["a", "b"]
    assert merged["status"] == "ordered" and merged["checked_out_at"] == "2026-11-05T18:00:00+00:00"
    assert merged["placed_at"] == "2026-11-05T18:00:00+00:00"
    assert merged["sent_at"] == ["2026-11-01T10:00:00+00:00", "2026-11-03T10:00:00+00:00"]
    assert merged["merged_from"] == ["a", "b"]
    assert merged["list_name"] == "Weekly + Extras"
    qty = {i["upc"]: i["quantity"] for i in merged["items"]}
    assert qty == {"1": 3, "2": 1}
    assert merged["item_count"] == 2 and merged["estimated_total"] == 4.5
    assert merged["store_name"] == "Kroger Hyde Park" and merged["cart"] == "kroger"


def test_merge_checkout_single_send_keeps_its_id():
    now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    merged, removed = merge_checkout([sent("a", "2026-11-01T10:00:00+00:00", ("1", "Bananas", 1, 0.5))], now=now)
    assert merged["id"] == "a" and removed == ["a"]
    assert merged["status"] == "ordered" and merged["placed_at"] == "2026-11-05T18:00:00+00:00"
    assert merged["sent_at"] == ["2026-11-01T10:00:00+00:00"] and "merged_from" not in merged


def test_merge_checkout_nothing_in_cart():
    assert merge_checkout([sent("c", "2026-10-01T10:00:00+00:00", ("3", "Bread", 1, 2.0), status="ordered")],
                          now=NOW) == (None, [])


RECEIPT = {
    "order_number": "112-345", "date": "2026-11-05", "total": 41.27, "savings": 6.10, "fuel_points": 41,
    "lines": [
        {"upc": "1", "name": "Bananas", "status": "received", "substitute": None, "quantity": 3, "price": 1.5},
        {"upc": "2", "name": "Milk", "status": "substituted", "substitute": "Organic Milk", "quantity": 1, "price": 4.2},
        {"upc": None, "name": "Bag fee", "status": "received", "substitute": None, "quantity": 1, "price": 0.1},
        {"upc": "9", "name": "Not on order", "status": "received", "substitute": None, "quantity": 1, "price": 1},
    ],
}


def test_apply_receipt_sets_header_and_outcomes():
    now = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    order = sent("a", "2026-11-05T18:00:00+00:00", ("1", "Bananas", 3, 0.5), ("2", "Milk", 1, 3.0), status="ordered")
    out = apply_receipt(order, RECEIPT, now=now)
    r = out["receipt"]
    assert r["order_number"] == "112-345" and r["date"] == "2026-11-05"
    assert r["total"] == 41.27 and r["savings"] == 6.1 and r["fuel_points"] == 41
    assert r["added_at"] == "2026-11-06T09:00:00+00:00"
    assert r["unmatched"] == ["Bag fee", "Not on order"]
    by = {i["upc"]: i for i in out["items"]}
    assert by["1"]["outcome"] == {"status": "received", "substitute": None, "quantity": 3, "price": 1.5}
    assert by["2"]["outcome"]["status"] == "substituted" and by["2"]["outcome"]["substitute"] == "Organic Milk"
    assert "outcome" not in order["items"][0]  # the input is not modified


def test_apply_receipt_second_paste_updates_only_what_it_mentions():
    now = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
    order = sent("a", "2026-11-05T18:00:00+00:00", ("1", "Bananas", 3, 0.5), ("2", "Milk", 1, 3.0), status="ordered")
    first = apply_receipt(order, RECEIPT, now=now)
    refund = {"order_number": None, "total": None,
              "lines": [{"upc": "1", "name": "Bananas", "status": "refunded", "quantity": 0, "price": -1.5}]}
    out = apply_receipt(first, refund, now=now)
    by = {i["upc"]: i for i in out["items"]}
    assert by["1"]["outcome"]["status"] == "refunded"
    assert by["2"]["outcome"]["status"] == "substituted"
    assert out["receipt"]["order_number"] == "112-345" and out["receipt"]["total"] == 41.27
    assert out["receipt"]["unmatched"] == ["Bag fee", "Not on order"]
