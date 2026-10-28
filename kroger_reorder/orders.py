# kroger_reorder/orders.py
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
