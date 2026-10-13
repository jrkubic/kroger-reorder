# app/storage.py
"""Plain-JSON persistence: named grocery lists, store config, and Kroger tokens."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LISTS_DIR = "lists"
LEGACY_LIST = "list.json"
DEFAULT_LIST_NAME = "Standard"

UNTOUCHED = " Showing an empty list; the file is untouched until you add an item."


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "list"


def _valid_slug(slug: str) -> bool:
    """Slugs become path components; only accept what slugify itself would produce."""
    return bool(slug) and slug == slugify(slug)


@dataclass
class Item:
    upc: str
    name: str
    size: str
    quantity: int = 1
    included: bool = True
    image: str | None = None
    price: float | None = None
    promo: float | None = None
    price_checked_at: str | None = None


@dataclass
class ListLoad:
    slug: str
    name: str
    items: list[Item]
    warning: str | None = None


def _float_or_none(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


def parse_items(raw: Any) -> list[Item]:
    """Turn a JSON array of item dicts into Items. Raises ValueError on anything unreadable."""
    if not isinstance(raw, list):
        raise ValueError("items should be a JSON array")
    try:
        return [
            Item(
                upc=str(e["upc"]),
                name=str(e.get("name", "")),
                size=str(e.get("size", "")),
                quantity=int(e.get("quantity", 1)),
                included=bool(e.get("included", True)),
                image=str(e["image"]) if e.get("image") else None,
                price=_float_or_none(e.get("price")),
                promo=_float_or_none(e.get("promo")),
                price_checked_at=str(e["price_checked_at"]) if e.get("price_checked_at") else None,
            )
            for e in raw
        ]
    except (TypeError, KeyError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError(f"an entry has unexpected or missing fields ({exc})") from exc


class Storage:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.lists_dir = self.data_dir / LISTS_DIR
        self.lists_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # re-entrant: save_list -> load_list -> _read

    # -- generic helpers ----------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _read(self, name: str) -> Any | None:
        """Parsed JSON, or None if the file is missing. Raises ValueError if corrupt."""
        path = self._path(name)
        with self._lock:  # on Windows, replace() fails while another thread has the file open
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} is not valid JSON: {exc}") from exc

    def _write(self, name: str, value: Any) -> None:
        path = self._path(name)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _backup(self, name: str) -> None:
        """Rename <name> to <name>.corrupt-<timestamp> next to the original."""
        path = self._path(name)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path.replace(path.with_name(f"{path.name}.corrupt-{stamp}"))

    # -- named lists ---------------------------------------------------------
    @staticmethod
    def _list_file(slug: str) -> str:
        return f"{LISTS_DIR}/{slug}.json"

    def list_slugs(self) -> list[str]:
        return sorted(p.stem for p in self.lists_dir.glob("*.json"))

    def list_lists(self) -> list[dict]:
        out = []
        with self._lock:  # a concurrent delete between glob and load would otherwise yield None
            for slug in self.list_slugs():
                loaded = self.load_list(slug)
                if loaded is None:
                    continue
                out.append({"slug": slug, "name": loaded.name, "count": len(loaded.items)})
        return sorted(out, key=lambda d: d["name"].lower())

    def load_list(self, slug: str) -> ListLoad | None:
        if not _valid_slug(slug):
            return None
        file = self._list_file(slug)
        try:
            raw = self._read(file)
        except ValueError as exc:
            return ListLoad(slug, slug, [], warning=f"{exc}.{UNTOUCHED}")
        if raw is None:
            return None
        if not isinstance(raw, dict) or "items" not in raw:
            return ListLoad(slug, slug, [], warning=f"{file} should be an object with a name and an items array.{UNTOUCHED}")
        name = str(raw.get("name") or slug)
        try:
            items = parse_items(raw["items"])
        except ValueError as exc:
            if "JSON array" in str(exc):
                return ListLoad(slug, name, [], warning=f"{file} should be an object with a name and an items array.{UNTOUCHED}")
            return ListLoad(slug, name, [], warning=f"{file} has an entry with unexpected or missing fields ({exc}).{UNTOUCHED}")
        return ListLoad(slug, name, items)

    def save_list(self, slug: str, name: str, items: list[Item]) -> None:
        if not _valid_slug(slug):
            raise KeyError(slug)
        file = self._list_file(slug)
        with self._lock:
            loaded = self.load_list(slug)
            if loaded is not None and loaded.warning and self._path(file).exists():
                self._backup(file)
            self._write(file, {"name": name, "items": [asdict(i) for i in items]})

    def create_list(self, name: str) -> str:
        with self._lock:
            base = slugify(name)
            slug, n = base, 2
            while self._path(self._list_file(slug)).exists():
                slug = f"{base}-{n}"
                n += 1
            self.save_list(slug, name, [])
            return slug

    def rename_list(self, slug: str, name: str) -> None:
        with self._lock:
            loaded = self.load_list(slug)
            if loaded is None:
                raise KeyError(slug)
            self.save_list(slug, name, loaded.items)

    def duplicate_list(self, slug: str, name: str) -> str:
        with self._lock:
            loaded = self.load_list(slug)
            if loaded is None:
                raise KeyError(slug)
            new = self.create_list(name)
            self.save_list(new, name, loaded.items)
            return new

    def delete_list(self, slug: str) -> None:
        if not _valid_slug(slug):
            raise KeyError(slug)
        with self._lock:
            self._path(self._list_file(slug)).unlink(missing_ok=True)
            if self.load_config().get("active_list") == slug:
                self.set_active_list(None)

    def active_list(self) -> str | None:
        slugs = self.list_slugs()
        active = self.load_config().get("active_list")
        if active in slugs:
            return active
        return slugs[0] if slugs else None

    def set_active_list(self, slug: str | None) -> None:
        with self._lock:
            config = self.load_config()
            if slug is None:
                config.pop("active_list", None)
            else:
                config["active_list"] = slug
            self.save_config(config)

    def ensure_ready(self) -> None:
        """Migrate a v1 list.json if present and guarantee at least one list exists."""
        with self._lock:
            self._migrate_legacy()
            if not self.list_slugs():
                self.set_active_list(self.create_list(DEFAULT_LIST_NAME))

    def _migrate_legacy(self) -> None:
        legacy = self._path(LEGACY_LIST)
        if not legacy.exists() or self.list_slugs():
            return
        try:
            items = parse_items(self._read(LEGACY_LIST))
        except ValueError:
            self._backup(LEGACY_LIST)
            return
        slug = slugify(DEFAULT_LIST_NAME)
        self.save_list(slug, DEFAULT_LIST_NAME, items)
        self.set_active_list(slug)
        legacy.replace(legacy.with_name(f"{LEGACY_LIST}.migrated"))

    # -- store config ------------------------------------------------------
    def load_config(self) -> dict:
        try:
            raw = self._read("config.json")
        except ValueError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def save_config(self, config: dict) -> None:
        self._write("config.json", config)

    # -- OAuth tokens ------------------------------------------------------
    def load_tokens(self) -> dict | None:
        try:
            raw = self._read("tokens.json")
        except ValueError:
            return None
        return raw if isinstance(raw, dict) else None

    def save_tokens(self, tokens: dict) -> None:
        self._write("tokens.json", tokens)

    def clear_tokens(self) -> None:
        self._path("tokens.json").unlink(missing_ok=True)
