# app/usage.py
"""Usage profile derived from order history: what the household buys, how often, and when last.

Nothing is stored; it is recomputed from the recorded orders each time it is needed, and it is
both shown on the Orders tab and handed to Claude as context for photo suggestions.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def usage_profile(orders: list[dict], now: datetime | None = None) -> list[dict]:
    """One row per UPC across every order, sorted by times ordered then most recent.

    Row: upc, name, times, avg_quantity, last_ordered (ISO or None), days_since_last (int or None),
    avg_days_between (float or None; needs two dated orders).
    """
    now = now or datetime.now(timezone.utc)
    by_upc: dict[str, dict] = {}
    for order in orders:
        when = _parse(order.get("placed_at"))
        for item in order.get("items", []):
            if not isinstance(item, dict) or not item.get("upc"):
                continue
            upc = str(item["upc"])
            row = by_upc.setdefault(upc, {"upc": upc, "name": "", "quantities": [], "dates": []})
            if item.get("name"):
                row["name"] = str(item["name"])
            try:
                row["quantities"].append(max(1, int(item.get("quantity", 1))))
            except (TypeError, ValueError):
                row["quantities"].append(1)
            if when:
                row["dates"].append(when)
    out = []
    for row in by_upc.values():
        dates = sorted(row["dates"])
        last = dates[-1] if dates else None
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
        out.append({
            "upc": row["upc"],
            "name": row["name"],
            "times": len(row["quantities"]),
            "avg_quantity": round(sum(row["quantities"]) / len(row["quantities"]), 1),
            "last_ordered": last.isoformat(timespec="seconds") if last else None,
            "days_since_last": int((now - last).total_seconds() // 86400) if last else None,
            "avg_days_between": round(sum(gaps) / len(gaps), 1) if gaps else None,
        })
    # Most-bought first; among equals, most recently bought first (stable sorts, applied in reverse priority).
    out.sort(key=lambda r: r["last_ordered"] or "", reverse=True)
    out.sort(key=lambda r: -r["times"])
    return out


def profile_text(profile: list[dict], limit: int = 60) -> str:
    """Compact lines for a prompt, e.g. 'whole milk: bought 6x, usually 1, last 12 days ago, about every 9 days'."""
    lines = []
    for row in profile[:limit]:
        bits = [f"bought {row['times']}x", f"usually {row['avg_quantity']:g}"]
        if row["days_since_last"] is not None:
            bits.append(f"last {row['days_since_last']} days ago")
        if row["avg_days_between"] is not None:
            bits.append(f"about every {row['avg_days_between']:g} days")
        lines.append(f"- {row['name'] or row['upc']}: " + ", ".join(bits))
    return "\n".join(lines)
