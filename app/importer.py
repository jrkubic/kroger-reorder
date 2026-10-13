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
MAX_QUANTITY = 99


@dataclass
class ParsedLine:
    raw: str
    name: str
    quantity: int = 1


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


def _line(raw: str, name: str, quantity: int | None = None) -> ParsedLine | None:
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
    if name_idx is not None:
        rows = rows[1:]
    else:
        name_idx, qty_idx = 0, 1
    out = []
    for row in rows:
        if name_idx >= len(row) or not row[name_idx].strip():
            continue
        qty = _to_qty(row[qty_idx]) if qty_idx is not None and qty_idx < len(row) and row[qty_idx].strip() else None
        line = _line(delimiter.join(row), row[name_idx], qty)
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
            line = _line(json.dumps(entry), name, _to_qty(qty_raw) if qty_raw is not None else None)
        else:
            line = None
        if line:
            out.append(line)
    return out
