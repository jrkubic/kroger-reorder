# kroger_reorder/orders.py
"""Order snapshots: what was sent to a cart (Kroger's, or since v7.2 the user's Aldi cart), recorded
after the store accepted it. Orders without a "cart" field predate v7.2 and are Kroger orders."""
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


def _estimate(snap: list[dict]) -> float | None:
    priced = [(i["quantity"], p) for i in snap if (p := _unit_price(i)) is not None]
    return round(sum(q * p for q, p in priced), 2) if priced else None


def build_order(items: list[dict], *, list_slug: str | None, list_name: str | None, store: dict,
                reorder_of: str | None = None, cart: str = "kroger", status: str | None = None,
                now: datetime | None = None) -> dict:
    """`status` is set on Kroger sends ("in_cart"); an order without one counts as ordered."""
    now = now or datetime.now(timezone.utc)
    snap = [snapshot_item(i) for i in items]
    order = {
        "id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}",
        "placed_at": now.isoformat(timespec="seconds"),
        "list_slug": list_slug,
        "list_name": list_name,
        "store_id": store.get("store_id"),
        "store_name": store.get("store_name"),
        "reorder_of": reorder_of,
        "cart": cart,
        "items": snap,
        "estimated_total": _estimate(snap),
        "item_count": len(snap),
    }
    if status:
        order["status"] = status
    return order


# -- v7.3: what the app put in the Kroger cart, checkout, receipts ------------------------------
IN_CART, ORDERED, DROPPED = "in_cart", "ordered", "dropped"
OUTCOMES = ("received", "substituted", "out_of_stock", "refunded")
RECEIPT_FIELDS = ("order_number", "date", "total", "savings", "fuel_points")


def is_ordered(order: dict) -> bool:
    """A purchase: checked out, or recorded before v7.3 / on Aldi (no status)."""
    return order.get("status", ORDERED) == ORDERED


def in_cart(orders: list[dict]) -> list[dict]:
    """Kroger sends not yet checked out or dropped, oldest first."""
    found = [o for o in orders if o.get("status") == IN_CART and o.get("cart", "kroger") == "kroger"]
    return sorted(found, key=lambda o: str(o.get("placed_at", "")))


def _quantity(item: dict) -> int:
    try:
        return max(0, int(item.get("quantity", 1) or 0))
    except (TypeError, ValueError):
        return 0


def _combine(orders: list[dict]) -> list[dict]:
    by_upc: dict[str, dict] = {}
    for order in orders:
        for item in order.get("items", []):
            if not isinstance(item, dict) or not item.get("upc"):
                continue
            upc = str(item["upc"])
            row = by_upc.get(upc)
            if row is None:
                row = by_upc[upc] = {**item, "upc": upc, "quantity": 0, "sent_at": []}
            row["quantity"] += _quantity(item)
            if order.get("placed_at") and order["placed_at"] not in row["sent_at"]:
                row["sent_at"].append(order["placed_at"])
    return list(by_upc.values())


def cart_contents(orders: list[dict]) -> list[dict]:
    """What this app has put in the Kroger cart since the last checkout, one row per UPC:
    the snapshot fields plus the summed quantity and `sent_at` (send times, oldest first)."""
    return _combine(in_cart(orders))


def merge_checkout(orders: list[dict], *, now: datetime | None = None) -> tuple[dict | None, list[str]]:
    """The in-cart sends become one ordered order (one real Kroger order). Returns it and the ids of
    the orders it replaces; (None, []) when nothing is in the cart. A single send keeps its id."""
    sends = in_cart(orders)
    if not sends:
        return None, []
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    ids = [o["id"] for o in sends]
    names: list[str] = []
    for o in sends:
        if o.get("list_name") and o["list_name"] not in names:
            names.append(o["list_name"])
    items = [{k: v for k, v in row.items() if k != "sent_at"} for row in _combine(sends)]
    first, last = sends[0], sends[-1]
    merged = {**first, "store_id": last.get("store_id"), "store_name": last.get("store_name"), "placed_at": stamp, "status": ORDERED, "checked_out_at": stamp,
              "sent_at": [o.get("placed_at") for o in sends], "items": items,
              "estimated_total": _estimate(items), "item_count": len(items),
              "list_name": " + ".join(names) or first.get("list_name")}
    if len(sends) > 1:
        merged["id"] = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"
        merged["merged_from"] = ids
        if len({o.get("list_slug") for o in sends}) > 1:
            merged["list_slug"] = None
        merged["reorder_of"] = None
    return merged, ids


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _units(value) -> int | None:
    n = _number(value)
    return max(0, round(n)) if n is not None else None


def _outcome_for(item: dict, lines: list[dict], prior: dict | None) -> dict:
    """One product's outcome from this paste's lines for it, applied on top of what an earlier paste
    said. received/substituted/out_of_stock lines describe the pickup and replace the earlier
    outcome; refunded lines take units off whatever was received (all of them when no quantity)."""
    pickup = [l for l in lines if l["status"] != "refunded"]
    refunds = [l for l in lines if l["status"] == "refunded"]
    if pickup:
        got = [l for l in pickup if l["status"] in ("received", "substituted")]
        substitutes = [l["substitute"] for l in got if l["status"] == "substituted" and l["substitute"]]
        quantity = sum(q for l in got if (q := _units(l.get("quantity"))) is not None) if got else 0
        if got and all(_units(l.get("quantity")) is None for l in got):
            quantity = None   # charged, but the receipt gave no count
        prices = [p for l in pickup if (p := _number(l.get("price"))) is not None]
        base = {"status": "substituted" if substitutes else "received" if got else "out_of_stock",
                "substitute": ", ".join(dict.fromkeys(substitutes)) or None,
                "quantity": quantity, "price": round(sum(prices), 2) if prices else None}
    elif prior:
        base = dict(prior)
    else:
        base = {"status": "received", "substitute": None, "quantity": _units(item.get("quantity")), "price": None}
    if refunds:
        have = base["quantity"] if base["quantity"] is not None else _units(item.get("quantity")) or 0
        counts = [_units(l.get("quantity")) for l in refunds]
        taken = have if any(c is None or c == 0 for c in counts) else min(have, sum(counts))
        base["refunded"] = (base.get("refunded") or 0) + taken
        base["quantity"] = have - taken
        if base["quantity"] == 0:
            base["status"] = "refunded"
    return base


def apply_receipt(order: dict, parsed: dict, *, now: datetime | None = None) -> dict:
    """A copy of `order` with what a receipt says: header fields that are present overwrite, and the
    lines for each product on the order (several lines can share one) set that item's `outcome`.
    Lines that match nothing are kept by name in `receipt.unmatched`. A later paste only changes the
    products it mentions, and a refund in it takes units off what was received."""
    now = now or datetime.now(timezone.utc)
    out = {**order, "items": [dict(i) for i in order.get("items", []) if isinstance(i, dict)]}
    receipt = dict(order.get("receipt") or {})
    for field in RECEIPT_FIELDS:
        value = parsed.get(field)
        if value is None or value == "":
            continue
        if field in ("total", "savings"):
            value = _number(value)
        elif field == "fuel_points":
            value = int(n) if (n := _number(value)) is not None else None
        else:
            value = str(value).strip()[:40] or None
        if value is not None:
            receipt[field] = value
    by_upc = {str(i.get("upc")): i for i in out["items"]}
    unmatched = list(receipt.get("unmatched") or [])
    lines_for: dict[str, list[dict]] = {}
    for line in parsed.get("lines") or []:
        if not isinstance(line, dict):
            continue
        status = str(line.get("status") or "").lower()
        upc = str(line.get("upc")) if line.get("upc") is not None else None
        if upc not in by_upc or status not in OUTCOMES:
            name = str(line.get("name") or "").strip()[:80]
            if name and name not in unmatched:
                unmatched.append(name)
            continue
        lines_for.setdefault(upc, []).append({**line, "status": status,
                                              "substitute": str(line.get("substitute") or "").strip()[:80] or None})
    for upc, lines in lines_for.items():
        item = by_upc[upc]
        prior = item.get("outcome") if isinstance(item.get("outcome"), dict) else None
        item["outcome"] = _outcome_for(item, lines, prior)
    receipt["unmatched"] = unmatched
    receipt["added_at"] = now.isoformat(timespec="seconds")
    out["receipt"] = receipt
    return out
