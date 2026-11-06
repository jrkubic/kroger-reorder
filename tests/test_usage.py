# tests/test_usage.py
from datetime import datetime, timezone

from kroger_reorder.usage import profile_text, usage_profile

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def order(placed_at, *items):
    return {"id": placed_at, "placed_at": placed_at,
            "items": [{"upc": u, "name": n, "quantity": q} for u, n, q in items]}


ORDERS = [
    order("2026-10-24T10:00:00+00:00", ("1", "Whole Milk", 1), ("2", "Eggs", 2)),
    order("2026-10-15T10:00:00+00:00", ("1", "Whole Milk", 1), ("3", "Bread", 1)),
    order("2026-10-06T10:00:00+00:00", ("1", "Whole Milk", 2)),
]


def test_profile_counts_averages_and_intervals():
    rows = {r["upc"]: r for r in usage_profile(ORDERS, now=NOW)}
    milk = rows["1"]
    assert milk["name"] == "Whole Milk" and milk["times"] == 3
    assert milk["avg_quantity"] == 1.3
    assert milk["last_ordered"] == "2026-10-24T10:00:00+00:00"
    assert milk["days_since_last"] == 2
    assert milk["avg_days_between"] == 9.0
    assert rows["2"]["avg_days_between"] is None and rows["2"]["times"] == 1


def test_profile_orders_most_bought_then_most_recent():
    assert [r["upc"] for r in usage_profile(ORDERS, now=NOW)] == ["1", "2", "3"]


def test_profile_tolerates_bad_dates_and_items():
    orders = [{"placed_at": "not a date", "items": [{"upc": "9", "name": "Tea", "quantity": "x"}, "junk", {"name": "no upc"}]}]
    (row,) = usage_profile(orders, now=NOW)
    assert row["times"] == 1 and row["avg_quantity"] == 1.0
    assert row["last_ordered"] is None and row["days_since_last"] is None


def test_profile_text_is_compact_and_capped():
    text = profile_text(usage_profile(ORDERS, now=NOW), limit=2)
    assert text.splitlines() == [
        "- Whole Milk: bought 3x, usually 1.3, last 2 days ago, about every 9 days",
        "- Eggs: bought 1x, usually 2, last 2 days ago",
    ]


def test_profile_text_empty():
    assert profile_text([]) == ""


def test_profile_skips_sends_not_checked_out():
    orders = [
        {**order("2026-10-24T10:00:00+00:00", ("1", "Whole Milk", 1)), "status": "in_cart"},
        {**order("2026-10-20T10:00:00+00:00", ("1", "Whole Milk", 1)), "status": "dropped"},
        {**order("2026-10-15T10:00:00+00:00", ("1", "Whole Milk", 2)), "status": "ordered"},
    ]
    rows = usage_profile(orders, now=NOW)
    assert len(rows) == 1 and rows[0]["times"] == 1 and rows[0]["avg_quantity"] == 2


def test_profile_uses_receipt_outcomes():
    o = order("2026-10-24T10:00:00+00:00", ("1", "Whole Milk", 2), ("2", "Eggs", 1), ("3", "Bread", 1), ("4", "Jam", 1))
    o["items"][0]["outcome"] = {"status": "substituted", "substitute": "Organic Milk", "quantity": 1, "price": 4}
    o["items"][1]["outcome"] = {"status": "out_of_stock", "substitute": None, "quantity": 0, "price": None}
    o["items"][2]["outcome"] = {"status": "refunded", "substitute": None, "quantity": 0, "price": None}
    rows = {r["upc"]: r for r in usage_profile([o], now=NOW)}
    assert set(rows) == {"1", "4"}
    assert rows["1"]["avg_quantity"] == 1


def test_profile_skips_items_fully_refunded_and_counts_partial():
    o = order("2026-10-24T10:00:00+00:00", ("1", "Whole Milk", 3), ("2", "Eggs", 2))
    o["items"][0]["outcome"] = {"status": "received", "substitute": None, "quantity": 2, "price": None, "refunded": 1}
    o["items"][1]["outcome"] = {"status": "received", "substitute": None, "quantity": 0, "price": None}
    rows = {r["upc"]: r for r in usage_profile([o], now=NOW)}
    assert set(rows) == {"1"} and rows["1"]["avg_quantity"] == 2
