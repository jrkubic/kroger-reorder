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
