# app/importer.py
"""Turn pasted text (CSV, JSON, Markdown, plain lines) into (name, quantity) lines."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])(?:\s+|$)")
CHECKBOX = re.compile(r"^\s*\[[ xX]\]\s*")
HEADING = re.compile(r"^\s*#")
LEADING_QTY = re.compile(r"^(\d+)\s*[xX×]\s+(.+)$")
TRAILING_QTY = re.compile(r"^(.+?)\s+[xX×]\s*(\d+)$")
PAREN_QTY = re.compile(r"^(.+?)\s*\((\d+)\)$")
NAME_KEYS = ("name", "item", "product", "description", "title")
QTY_KEYS = ("quantity", "qty", "count", "amount")
# Columns an exported CSV (or a list JSON file) carries. A row with a UPC is an exact product and
# skips both quantity-in-name parsing and the Kroger search.
UPC_KEY = "upc"
EXACT_KEYS = ("size", "included", "price", "promo", "image")
FALSE_WORDS = {"false", "0", "no", "n", "off", ""}
MAX_QUANTITY = 99


@dataclass
class ParsedLine:
    raw: str
    name: str
    quantity: int = 1
    exact: dict | None = None  # {upc, size, included, price, promo, image} when the source had a UPC


def parse(text: str) -> list[ParsedLine]:
    text = text.lstrip("\ufeff").strip()
    if not text:
        return []
    if text[0] in "[{":
        parsed = _parse_json(text)
        if parsed is not None:
            return parsed
    lines = [ln for ln in text.splitlines() if ln.strip()]
    delimiter = _looks_like_csv(lines)
    if delimiter:
        return _parse_csv(text, delimiter)
    return _parse_lines(lines)


# -- helpers ---------------------------------------------------------------------

def _split_quantity(name: str) -> tuple[str, int]:
    for pattern, name_group, qty_group in ((LEADING_QTY, 2, 1), (TRAILING_QTY, 1, 2), (PAREN_QTY, 1, 2)):
        m = pattern.match(name)
        if m:
            return m.group(name_group).strip(), _clamp(int(m.group(qty_group)))
    return name.strip(), 1


def _clamp(quantity: int) -> int:
    return min(MAX_QUANTITY, max(1, quantity))


def _to_qty(value: Any) -> int:
    try:
        return _clamp(int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip()) if value is not None and str(value).strip() != "" else None
    except ValueError:
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in FALSE_WORDS if value is not None else True


def _exact(fields: dict[str, Any]) -> dict | None:
    """Normalise the product columns of a row that carries a UPC; None when it doesn't."""
    upc = str(fields.get(UPC_KEY) or "").strip()
    if not upc:
        return None
    return {
        "upc": upc,
        "size": str(fields.get("size") or "").strip(),
        "included": _to_bool(fields.get("included", True)),
        "price": _to_float(fields.get("price")),
        "promo": _to_float(fields.get("promo")),
        "image": (str(fields["image"]).strip() or None) if fields.get("image") else None,
    }


def _line(raw: str, name: str, quantity: int | None = None, exact: dict | None = None) -> ParsedLine | None:
    if exact:
        name = name.strip()  # an exact product keeps its catalog name; "(12)" there is not a quantity
        return ParsedLine(raw=raw, name=name, quantity=quantity or 1, exact=exact) if name else None
    name, found = _split_quantity(name)
    if not name:
        return None
    return ParsedLine(raw=raw, name=name, quantity=quantity if quantity is not None else found)


def _parse_lines(lines: list[str]) -> list[ParsedLine]:
    out = []
    for raw in lines:
        if HEADING.match(raw):
            continue
        cleaned = CHECKBOX.sub("", BULLET.sub("", raw)).strip()
        if cleaned and (line := _line(raw, cleaned)):
            out.append(line)
    return out


def _looks_like_csv(lines: list[str]) -> str | None:
    """The delimiter ("\t" or ",") if most lines are delimited rows, else None."""
    if any(BULLET.match(ln) or CHECKBOX.match(ln) or HEADING.match(ln) for ln in lines):
        return None
    needed = max(1, len(lines) // 2 + 1) if len(lines) > 1 else 1
    for delimiter in ("\t", ","):  # tabs win: a spreadsheet paste may have commas inside cells
        if sum(1 for ln in lines if delimiter in ln) >= needed:
            return delimiter
    return None


def _parse_csv(text: str, delimiter: str = ",") -> list[ParsedLine]:
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(cell.strip() for cell in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    name_idx = next((i for i, c in enumerate(header) if c in NAME_KEYS), None)
    qty_idx = next((i for i, c in enumerate(header) if c in QTY_KEYS), None)
    extra_idx: dict[str, int] = {}
    if name_idx is not None:
        rows = rows[1:]
        extra_idx = {key: header.index(key) for key in (UPC_KEY, *EXACT_KEYS) if key in header}
    else:
        name_idx, qty_idx = 0, 1

    def cell(row: list[str], idx: int | None) -> str | None:
        return row[idx] if idx is not None and idx < len(row) and row[idx].strip() else None

    out = []
    for row in rows:
        if name_idx >= len(row) or not row[name_idx].strip():
            continue
        raw_qty = cell(row, qty_idx)
        qty = _to_qty(raw_qty) if raw_qty is not None else None
        exact = _exact({key: cell(row, idx) for key, idx in extra_idx.items()}) if extra_idx else None
        line = _line(delimiter.join(row), row[name_idx], qty, exact)
        if line:
            out.append(line)
    return out


def _parse_json(text: str) -> list[ParsedLine] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        return None
    out = []
    for entry in data:
        if isinstance(entry, str):
            line = _line(entry, entry)
        elif isinstance(entry, dict):
            name = next(
                (str(entry[k]) for k in NAME_KEYS if entry.get(k) and isinstance(entry[k], (str, int, float))), ""
            )
            qty_raw = next((entry[k] for k in QTY_KEYS if entry.get(k) is not None), None)
            line = _line(json.dumps(entry), name, _to_qty(qty_raw) if qty_raw is not None else None, _exact(entry))
        else:
            line = None
        if line:
            out.append(line)
    return out
