# Kroger Reorder v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add named lists (with migration from v1), product images, sale badges with price refresh, LAN access, and CSV/JSON/Markdown import with a review step to the existing Kroger Reorder tool.

**Architecture:** Storage becomes list-aware with one JSON file per list under `data/lists/`. The Kroger client returns images and gains a single-product lookup for price refresh. A new pure `importer.py` parses pasted text into `(name, quantity)` lines. Routes are re-scoped under `/api/lists/{slug}`. The page's JS moves to `app/static/app.js` and gains a list selector, thumbnails, badges, and an import dialog.

**Tech Stack:** Unchanged. Python 3.14, FastAPI, httpx, pytest, vanilla JS. No new dependencies.

**Design doc:** `docs/plans/2026-10-12-v2-lists-images-import-design.md`. Read it first.

**Current state:** v1 is merged on `main` (commit `3961076`), 66 tests pass. Files you will touch: `app/storage.py`, `app/kroger.py`, `app/main.py`, `app/__main__.py`, `app/static/index.html`, `README.md`, and all four test files. New: `app/importer.py`, `app/static/app.js`, `tests/test_importer.py`.

**Conventions:**
- Work on branch `feat/v2-lists` from `main`.
- Run from `C:\Projects\kroger-reorder`; `.venv/Scripts/python -m pytest -q`.
- Write whole files with the Write tool (heredocs are too long on Windows).
- Commit messages end with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- TDD per task: tests first, see them fail, implement, see them pass, commit.

---

### Task 1: Storage — named lists, optional item fields, migration

This is a rewrite of the list half of `app/storage.py` and of `tests/test_storage.py`. Config/tokens code is unchanged.

**Files:**
- Replace: `app/storage.py`
- Replace: `tests/test_storage.py`

**Step 1: Replace the test file**

```python
# tests/test_storage.py
import json
import threading

from app.storage import Item, Storage, slugify


def milk(**over):
    base = dict(upc="0001", name="Milk", size="1 gal")
    base.update(over)
    return Item(**base)


# -- slugs ------------------------------------------------------------------

def test_slugify_basic_and_fallback():
    assert slugify("Weekly Staples!") == "weekly-staples"
    assert slugify("  Taco  Night ") == "taco-night"
    assert slugify("???") == "list"


# -- create / load / save ---------------------------------------------------

def test_create_list_writes_empty_file_and_returns_slug(tmp_path):
    s = Storage(tmp_path)
    slug = s.create_list("Weekly Staples")
    assert slug == "weekly-staples"
    raw = json.loads((tmp_path / "lists" / "weekly-staples.json").read_text(encoding="utf-8"))
    assert raw == {"name": "Weekly Staples", "items": []}


def test_slug_collisions_get_numeric_suffix(tmp_path):
    s = Storage(tmp_path)
    assert s.create_list("Weekly") == "weekly"
    assert s.create_list("weekly!") == "weekly-2"
    assert s.create_list("WEEKLY") == "weekly-3"


def test_save_and_load_roundtrip_with_optional_fields(tmp_path):
    s = Storage(tmp_path)
    items = [
        milk(quantity=2, included=False, image="https://img/x.jpg", price=3.49, promo=2.99,
             price_checked_at="2026-10-12T14:30:00"),
        Item(upc="0002", name="Eggs", size="12 ct"),
    ]
    s.save_list("weekly", "Weekly", items)
    loaded = s.load_list("weekly")
    assert loaded.slug == "weekly"
    assert loaded.name == "Weekly"
    assert loaded.items == items
    assert loaded.warning is None
    assert loaded.items[1].image is None and loaded.items[1].price is None


def test_load_missing_list_is_none(tmp_path):
    assert Storage(tmp_path).load_list("nope") is None


def test_optional_fields_default_when_absent_from_file(tmp_path):
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "old.json").write_text(
        json.dumps({"name": "Old", "items": [{"upc": "1", "name": "A", "size": "", "quantity": "2"}]}),
        encoding="utf-8",
    )
    loaded = Storage(tmp_path).load_list("old")
    assert loaded.warning is None
    assert loaded.items[0].quantity == 2
    assert loaded.items[0].image is None
    assert loaded.items[0].promo is None


def test_corrupt_list_file_warns_and_is_untouched(tmp_path):
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "bad.json").write_text("{not json", encoding="utf-8")
    loaded = Storage(tmp_path).load_list("bad")
    assert loaded.items == []
    assert loaded.name == "bad"
    assert "not valid JSON" in loaded.warning
    assert (tmp_path / "lists" / "bad.json").read_text(encoding="utf-8") == "{not json"


def test_list_file_without_items_array_warns(tmp_path):
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "bad.json").write_text('{"name": "Bad", "items": "x"}', encoding="utf-8")
    loaded = Storage(tmp_path).load_list("bad")
    assert loaded.items == []
    assert "items array" in loaded.warning


def test_list_entry_with_bad_fields_warns_and_keeps_name(tmp_path):
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "bad.json").write_text(
        json.dumps({"name": "Bad", "items": [{"upc": "1", "name": "A", "size": "", "quantity": "two"}]}),
        encoding="utf-8",
    )
    loaded = Storage(tmp_path).load_list("bad")
    assert loaded.items == []
    assert loaded.name == "Bad"
    assert "unexpected or missing fields" in loaded.warning


def test_save_list_backs_up_unreadable_file_first(tmp_path):
    s = Storage(tmp_path)
    (tmp_path / "lists" / "bad.json").write_text("{not json", encoding="utf-8")
    s.save_list("bad", "Bad", [milk()])
    backups = list((tmp_path / "lists").glob("bad.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json"
    assert s.load_list("bad").items == [milk()]


# -- rename / duplicate / delete / listing ----------------------------------

def test_rename_keeps_slug_and_items(tmp_path):
    s = Storage(tmp_path)
    s.save_list("weekly", "Weekly", [milk()])
    s.rename_list("weekly", "Weekly Staples")
    loaded = s.load_list("weekly")
    assert loaded.name == "Weekly Staples"
    assert loaded.items == [milk()]
    assert s.list_slugs() == ["weekly"]


def test_duplicate_copies_items_under_new_slug(tmp_path):
    s = Storage(tmp_path)
    s.save_list("weekly", "Weekly", [milk()])
    new = s.duplicate_list("weekly", "Weekly copy")
    assert new == "weekly-copy"
    assert s.load_list(new).items == [milk()]
    assert s.load_list("weekly").items == [milk()]


def test_delete_list_removes_file_and_clears_active(tmp_path):
    s = Storage(tmp_path)
    s.save_list("a", "A", [])
    s.save_list("b", "B", [])
    s.set_active_list("a")
    s.delete_list("a")
    assert s.list_slugs() == ["b"]
    assert s.active_list() == "b"
    assert "active_list" not in s.load_config()


def test_active_list_falls_back_to_first_when_unset_or_missing(tmp_path):
    s = Storage(tmp_path)
    assert s.active_list() is None
    s.save_list("b", "B", [])
    s.save_list("a", "A", [])
    assert s.active_list() == "a"  # alphabetical by slug
    s.set_active_list("b")
    assert s.active_list() == "b"
    s.set_active_list("ghost")
    assert s.active_list() == "a"


def test_list_lists_sorted_by_name_with_counts(tmp_path):
    s = Storage(tmp_path)
    s.save_list("z", "Alpha", [milk(), Item(upc="2", name="E", size="")])
    s.save_list("a", "Zulu", [])
    (tmp_path / "lists" / "bad.json").write_text("nope", encoding="utf-8")
    assert s.list_lists() == [
        {"slug": "z", "name": "Alpha", "count": 2},
        {"slug": "bad", "name": "bad", "count": 0},
        {"slug": "a", "name": "Zulu", "count": 0},
    ]


# -- migration / ensure_ready -----------------------------------------------

def test_ensure_ready_migrates_v1_list_json(tmp_path):
    legacy = [{"upc": "0001", "name": "Milk", "size": "1 gal", "quantity": 2, "included": False}]
    (tmp_path / "list.json").write_text(json.dumps(legacy), encoding="utf-8")
    s = Storage(tmp_path)
    s.ensure_ready()
    assert s.list_slugs() == ["standard"]
    loaded = s.load_list("standard")
    assert loaded.name == "Standard"
    assert loaded.items == [milk(quantity=2, included=False)]
    assert s.active_list() == "standard"
    assert not (tmp_path / "list.json").exists()
    assert (tmp_path / "list.json.migrated").exists()


def test_ensure_ready_backs_up_corrupt_legacy_and_creates_default(tmp_path):
    (tmp_path / "list.json").write_text("{bad", encoding="utf-8")
    s = Storage(tmp_path)
    s.ensure_ready()
    assert s.list_slugs() == ["standard"]
    assert s.load_list("standard").items == []
    assert list(tmp_path.glob("list.json.corrupt-*"))


def test_ensure_ready_creates_default_when_nothing_exists_and_is_idempotent(tmp_path):
    s = Storage(tmp_path)
    s.ensure_ready()
    s.ensure_ready()
    assert s.list_slugs() == ["standard"]


def test_ensure_ready_does_not_migrate_when_lists_already_exist(tmp_path):
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    s = Storage(tmp_path)
    s.save_list("weekly", "Weekly", [])
    s.ensure_ready()
    assert s.list_slugs() == ["weekly"]
    assert (tmp_path / "list.json").exists()


# -- config / tokens (unchanged behavior) -----------------------------------

def test_config_roundtrip_and_default(tmp_path):
    s = Storage(tmp_path)
    assert s.load_config() == {}
    s.save_config({"store_id": "01400943", "store_name": "Kroger Hyde Park"})
    assert s.load_config()["store_id"] == "01400943"


def test_non_object_config_is_empty(tmp_path):
    (tmp_path / "config.json").write_text("[1]", encoding="utf-8")
    assert Storage(tmp_path).load_config() == {}


def test_tokens_roundtrip_and_clear(tmp_path):
    s = Storage(tmp_path)
    assert s.load_tokens() is None
    s.save_tokens({"access_token": "a", "refresh_token": "r", "expires_at": 123.0})
    assert s.load_tokens()["refresh_token"] == "r"
    s.clear_tokens()
    assert s.load_tokens() is None
    s.clear_tokens()


# -- concurrency ---------------------------------------------------------------

def _run_threads(workers):
    errors = []

    def wrap(fn):
        def run():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        return run

    threads = [threading.Thread(target=wrap(w)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_writes_do_not_crash(tmp_path):
    s = Storage(tmp_path)
    errors = _run_threads([lambda i=i: s.save_config({"n": i}) for i in range(8)])
    assert errors == []
    assert s.load_config()["n"] in range(8)
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_reads_and_writes_do_not_crash(tmp_path):
    s = Storage(tmp_path)
    s.save_list("w", "W", [milk()])

    def reader():
        for _ in range(50):
            s.load_list("w")

    def writer():
        for i in range(50):
            s.save_list("w", "W", [milk(quantity=i + 1)])

    errors = _run_threads([reader, reader, reader, writer])
    assert errors == []
```

**Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_storage.py -q`
Expected: ImportError on `slugify` (collection error).

**Step 3: Replace `app/storage.py`**

```python
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
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
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
        for slug in self.list_slugs():
            loaded = self.load_list(slug)
            out.append({"slug": slug, "name": loaded.name, "count": len(loaded.items)})
        return sorted(out, key=lambda d: d["name"].lower())

    def load_list(self, slug: str) -> ListLoad | None:
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
```

Note on `test_ensure_ready_backs_up_corrupt_legacy_and_creates_default`: `_migrate_legacy` backs up and returns; `ensure_ready` then sees no lists and creates "Standard". That is the intended flow.

**Step 4: Run storage tests**

Run: `.venv/Scripts/python -m pytest tests/test_storage.py -q`
Expected: `25 passed`. (Other test files will now fail to import or call the old API; that is expected until Tasks 2 and 4.)

**Step 5: Commit**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat(storage): named lists with slugs, optional item fields, v1 migration

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Kroger client — images, regular/promo split, product lookup

**Files:**
- Modify: `app/kroger.py` (`search_products` and a new `lookup_product`)
- Modify: `tests/test_kroger.py` (one test rewritten, one added)

**Step 1: Update the tests**

Replace `test_search_products_parses_and_prefers_promo_price` with:

```python
PRODUCT_MILK = {
    "productId": "0001111041700", "upc": "0001111041700", "description": "Kroger Whole Milk", "brand": "Kroger",
    "images": [
        {"perspective": "back", "sizes": [{"size": "medium", "url": "https://img/back-m.jpg"}]},
        {"perspective": "front", "featured": True, "sizes": [
            {"size": "large", "url": "https://img/front-l.jpg"},
            {"size": "medium", "url": "https://img/front-m.jpg"},
        ]},
    ],
    "items": [{"size": "1 gal", "price": {"regular": 3.49, "promo": 2.99}}],
}


def test_search_products_parses_price_promo_and_front_image(client, server):
    server.queue(200, TOKEN)
    server.queue(200, {"data": [
        PRODUCT_MILK,
        {"upc": "0001111041701", "description": "Kroger 2% Milk", "brand": "Kroger",
         "images": [{"perspective": "left", "sizes": [{"size": "small", "url": "https://img/left-s.jpg"}]}],
         "items": [{"size": "1 gal", "price": {"regular": 3.49, "promo": 0}}]},
        {"upc": "0001111041702", "description": "Mystery Item", "items": []},
    ]})
    results = client.search_products("milk", "01400943")
    assert results == [
        {"upc": "0001111041700", "name": "Kroger Whole Milk", "brand": "Kroger", "size": "1 gal",
         "price": 3.49, "promo": 2.99, "image": "https://img/front-m.jpg"},
        {"upc": "0001111041701", "name": "Kroger 2% Milk", "brand": "Kroger", "size": "1 gal",
         "price": 3.49, "promo": None, "image": "https://img/left-s.jpg"},
        {"upc": "0001111041702", "name": "Mystery Item", "brand": "", "size": "",
         "price": None, "promo": None, "image": None},
    ]
    req = server.requests[1]
    assert req.url.path == "/v1/products"
    assert req.url.params["filter.term"] == "milk"
    assert req.url.params["filter.locationId"] == "01400943"


def test_lookup_product_parses_single_product(client, server):
    server.queue(200, TOKEN)
    server.queue(200, {"data": PRODUCT_MILK})
    result = client.lookup_product("0001111041700", "01400943")
    assert result["upc"] == "0001111041700"
    assert result["price"] == 3.49
    assert result["promo"] == 2.99
    assert result["image"] == "https://img/front-m.jpg"
    req = server.requests[1]
    assert req.url.path == "/v1/products/0001111041700"
    assert req.url.params["filter.locationId"] == "01400943"
```

**Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: 2 failures (shape mismatch; `AttributeError: lookup_product`).

**Step 3: Implement**

Replace `search_products` in `app/kroger.py` with the three methods below (keep everything else):

```python
    @staticmethod
    def _image_url(product: dict) -> str | None:
        """Medium front image if available, else the first image of any size."""
        images = product.get("images") or []
        ordered = sorted(images, key=lambda im: 0 if im.get("perspective") == "front" or im.get("featured") else 1)
        for image in ordered:
            sizes = image.get("sizes") or []
            for size in sizes:
                if size.get("size") == "medium" and size.get("url"):
                    return size["url"]
            for size in sizes:
                if size.get("url"):
                    return size["url"]
        return None

    def _summarize(self, product: dict) -> dict:
        item = (product.get("items") or [{}])[0]
        price = item.get("price") or {}
        return {
            "upc": product["upc"],
            "name": product.get("description", ""),
            "brand": product.get("brand", ""),
            "size": item.get("size", ""),
            "price": price.get("regular"),
            "promo": price.get("promo") or None,
            "image": self._image_url(product),
        }

    def search_products(self, term: str, location_id: str) -> list[dict]:
        body = self._get(
            "/products",
            self.app_token(),
            {"filter.term": term, "filter.locationId": location_id, "filter.limit": 10},
        )
        return [self._summarize(p) for p in body.get("data", [])]

    def lookup_product(self, upc: str, location_id: str) -> dict:
        body = self._get(f"/products/{upc}", self.app_token(), {"filter.locationId": location_id})
        return self._summarize(body["data"])
```

**Step 4: Run**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: `29 passed`.

**Step 5: Commit**

```bash
git add app/kroger.py tests/test_kroger.py
git commit -m "feat(kroger): product images, regular/promo prices, single product lookup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Import parser (pure functions)

**Files:**
- Create: `app/importer.py`
- Create: `tests/test_importer.py`

**Step 1: Write the tests**

```python
# tests/test_importer.py
from app.importer import ParsedLine, parse


def names(lines):
    return [(l.name, l.quantity) for l in lines]


def test_empty_text_gives_nothing():
    assert parse("") == []
    assert parse("   \n\n") == []


def test_plain_lines_default_quantity_one():
    assert names(parse("milk\neggs\n\nbread")) == [("milk", 1), ("eggs", 1), ("bread", 1)]


def test_markdown_bullets_checkboxes_numbers_and_headings():
    text = "# Groceries\n- [ ] milk\n* eggs\n1. bread\n- [x] butter\n## Dairy\n"
    assert names(parse(text)) == [("milk", 1), ("eggs", 1), ("bread", 1), ("butter", 1)]


def test_quantity_patterns():
    text = "2x milk\neggs x3\nbread (4)\n5 X yogurt\n2% milk"
    assert names(parse(text)) == [("milk", 2), ("eggs", 3), ("bread", 4), ("yogurt", 5), ("2% milk", 1)]


def test_raw_is_preserved():
    line = parse("- [ ] 2x milk")[0]
    assert line == ParsedLine(raw="- [ ] 2x milk", name="milk", quantity=2)


def test_csv_with_header():
    text = "name,quantity\nmilk,2\neggs,1\nbread,\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 1), ("bread", 1)]


def test_csv_without_header_uses_first_two_columns():
    text = "milk,2\neggs,3\nbutter,x\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 3), ("butter", 1)]


def test_csv_header_columns_in_any_order():
    text = "qty,item\n2,milk\n1,eggs\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 1)]


def test_json_array_of_strings():
    assert names(parse('["milk", "2x eggs"]')) == [("milk", 1), ("eggs", 2)]


def test_json_array_of_objects():
    text = '[{"name": "milk", "quantity": 2}, {"name": "eggs"}, {"item": "bread", "qty": "3"}]'
    assert names(parse(text)) == [("milk", 2), ("eggs", 1), ("bread", 3)]


def test_json_object_with_items_key():
    text = '{"name": "Weekly", "items": [{"name": "milk", "quantity": 2}]}'
    assert names(parse(text)) == [("milk", 2)]


def test_invalid_json_falls_back_to_lines():
    assert names(parse("[milk\neggs")) == [("[milk", 1), ("eggs", 1)]


def test_bulleted_lines_with_commas_are_not_csv():
    text = "- milk, whole\n- eggs, large"
    assert names(parse(text)) == [("milk, whole", 1), ("eggs, large", 1)]
```

**Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_importer.py -q`
Expected: `ModuleNotFoundError: No module named 'app.importer'`.

**Step 3: Implement**

```python
# app/importer.py
"""Turn pasted text (CSV, JSON, Markdown, plain lines) into (name, quantity) lines."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
CHECKBOX = re.compile(r"^\s*\[[ xX]\]\s*")
HEADING = re.compile(r"^\s*#")
LEADING_QTY = re.compile(r"^(\d+)\s*[xX×]\s+(.+)$")
TRAILING_QTY = re.compile(r"^(.+?)\s+[xX×]\s*(\d+)$")
PAREN_QTY = re.compile(r"^(.+?)\s*\((\d+)\)$")
NAME_KEYS = ("name", "item", "product", "description", "title")
QTY_KEYS = ("quantity", "qty", "count", "amount")


@dataclass
class ParsedLine:
    raw: str
    name: str
    quantity: int = 1


def parse(text: str) -> list[ParsedLine]:
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":
        parsed = _parse_json(text)
        if parsed is not None:
            return parsed
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if _looks_like_csv(lines):
        return _parse_csv(text)
    return _parse_lines(lines)


# -- helpers ---------------------------------------------------------------------

def _split_quantity(name: str) -> tuple[str, int]:
    for pattern, name_group, qty_group in ((LEADING_QTY, 2, 1), (TRAILING_QTY, 1, 2), (PAREN_QTY, 1, 2)):
        m = pattern.match(name)
        if m:
            return m.group(name_group).strip(), max(1, int(m.group(qty_group)))
    return name.strip(), 1


def _to_qty(value: Any) -> int:
    try:
        return max(1, int(str(value).strip()))
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


def _looks_like_csv(lines: list[str]) -> bool:
    if any(BULLET.match(ln) or CHECKBOX.match(ln) or HEADING.match(ln) for ln in lines):
        return False
    with_comma = sum(1 for ln in lines if "," in ln)
    return with_comma >= max(1, len(lines) // 2 + 1) if len(lines) > 1 else "," in lines[0]


def _parse_csv(text: str) -> list[ParsedLine]:
    rows = [r for r in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in r)]
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
        line = _line(",".join(row), row[name_idx], qty)
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
            name = next((str(entry[k]) for k in NAME_KEYS if entry.get(k)), "")
            qty_raw = next((entry[k] for k in QTY_KEYS if entry.get(k) is not None), None)
            line = _line(json.dumps(entry), name, _to_qty(qty_raw) if qty_raw is not None else None)
        else:
            line = None
        if line:
            out.append(line)
    return out
```

**Step 4: Run**

Run: `.venv/Scripts/python -m pytest tests/test_importer.py -q`
Expected: `13 passed`. If a regex case fails, fix the regex, not the test; the tests define the contract.

**Step 5: Commit**

```bash
git add app/importer.py tests/test_importer.py
git commit -m "feat: import parser for CSV, JSON, Markdown, and plain text lines

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Routes — list CRUD, scoped items and push, prices, import

Full rewrite of `app/main.py` and `tests/test_routes.py`.

**Files:**
- Replace: `app/main.py`
- Replace: `tests/test_routes.py`

**Step 1: Replace the test file**

```python
# tests/test_routes.py
import json

import pytest
from fastapi.testclient import TestClient

from app.kroger import KrogerError, NotSignedIn
from app.main import create_app
from app.storage import Item, Storage


def product(upc, name, **over):
    base = {"upc": upc, "name": name, "brand": "Kroger", "size": "1 gal", "price": 3.49, "promo": None,
            "image": f"https://img/{upc}.jpg"}
    base.update(over)
    return base


class FakeKroger:
    def __init__(self):
        self.signed_in = False
        self.exchanged_code = None
        self.exchanged_state = None
        self.cart_calls = []
        self.cart_error: Exception | None = None
        self.lookups = {}       # upc -> product dict or Exception
        self.lookup_calls = []

    def is_signed_in(self):
        return self.signed_in

    def find_stores(self, zip_code):
        return [{"id": "01400943", "name": "Kroger Hyde Park", "address": "3760 Paxton Ave, Cincinnati"}]

    def search_products(self, term, location_id):
        return [product("0001", f"{term} at {location_id}", promo=2.99),
                product("0009", f"{term} alt")]

    def lookup_product(self, upc, location_id):
        self.lookup_calls.append(upc)
        result = self.lookups.get(upc)
        if isinstance(result, Exception):
            raise result
        return result or product(upc, f"Looked up {upc}", price=5.0, promo=4.0)

    def authorize_url(self):
        return "https://api.kroger.com/v1/connect/oauth2/authorize?fake=1"

    def exchange_code(self, code, state):
        self.exchanged_code = code
        self.exchanged_state = state
        self.signed_in = True

    def add_to_cart(self, items):
        if self.cart_error:
            raise self.cart_error
        self.cart_calls.append(items)


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path)


@pytest.fixture
def kroger():
    return FakeKroger()


@pytest.fixture
def client(storage, kroger):
    return TestClient(create_app(storage, kroger), follow_redirects=False)


@pytest.fixture
def with_store(storage):
    storage.save_config({**storage.load_config(), "store_id": "01400943", "store_name": "Kroger Hyde Park"})


MILK = {"upc": "0001", "name": "Whole Milk", "size": "1 gal"}
EGGS = {"upc": "0002", "name": "Eggs", "size": "12 ct"}
FULL = {"quantity": 1, "included": True, "image": None, "price": None, "promo": None, "price_checked_at": None}


# -- state / setup ---------------------------------------------------------------

def test_initial_state_has_default_list(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json() == {
        "store": None, "signed_in": False,
        "lists": [{"slug": "standard", "name": "Standard", "count": 0}],
        "active_list": "standard",
    }


def test_find_stores(client):
    r = client.get("/api/stores", params={"zip": "45209"})
    assert r.json()["stores"][0]["id"] == "01400943"


def test_choose_store_persists_and_keeps_active_list(client, storage):
    r = client.put("/api/store", json={"id": "01400943", "name": "Kroger Hyde Park"})
    assert r.status_code == 200
    assert storage.load_config()["store_id"] == "01400943"
    assert client.get("/api/state").json()["active_list"] == "standard"


def test_login_redirects_to_kroger(client):
    r = client.get("/auth/login")
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.kroger.com/v1/connect/oauth2/authorize")


def test_callback_exchanges_code_and_redirects_home(client, kroger):
    r = client.get("/callback", params={"code": "abc", "state": "s"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"
    assert (kroger.exchanged_code, kroger.exchanged_state) == ("abc", "s")
    assert client.get("/api/state").json()["signed_in"] is True


def test_callback_denied_redirects_home_with_error(client, kroger):
    r = client.get("/callback", params={"error": "access_denied"})
    assert r.headers["location"] == "/?auth_error=access_denied"
    assert kroger.exchanged_code is None


def test_callback_exchange_failure_redirects_home_with_error(client, kroger):
    def boom(code, state):
        raise KrogerError(400, "invalid_grant")
    kroger.exchange_code = boom
    r = client.get("/callback", params={"code": "x", "state": "s"})
    assert r.headers["location"] == "/?auth_error=invalid_grant"


def test_search_requires_store(client):
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 400
    assert "store" in r.json()["error"].lower()


def test_search_returns_images_and_promo(client, with_store):
    r = client.get("/api/search", params={"term": "milk"})
    first = r.json()["results"][0]
    assert first["image"] == "https://img/0001.jpg"
    assert first["promo"] == 2.99


def test_kroger_error_becomes_502_with_message(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(429, "rate limited")
    kroger.search_products = boom
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: rate limited"}


def test_transport_error_is_502_without_kroger_said_prefix(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(0, "Could not reach Kroger: ConnectError")
    kroger.search_products = boom
    assert client.get("/api/search", params={"term": "milk"}).json() == {"error": "Could not reach Kroger: ConnectError"}


def test_index_and_static_js_are_served(client):
    assert "text/html" in client.get("/").headers["content-type"]
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_validation_error_uses_error_shape(client):
    r = client.post("/api/lists/standard/items", json={"upc": "1"})
    assert r.status_code == 422
    assert "name" in r.json()["error"]


# -- list CRUD ---------------------------------------------------------------------

def test_create_list_and_switch_active(client):
    r = client.post("/api/lists", json={"name": "Taco Night"})
    assert r.status_code == 200
    assert r.json() == {"slug": "taco-night"}
    r = client.put("/api/active", json={"slug": "taco-night"})
    assert r.json() == {"active_list": "taco-night"}
    state = client.get("/api/state").json()
    assert [l["slug"] for l in state["lists"]] == ["standard", "taco-night"]
    assert state["active_list"] == "taco-night"


def test_create_list_rejects_blank_name(client):
    assert client.post("/api/lists", json={"name": "   "}).status_code == 400


def test_rename_list(client):
    r = client.patch("/api/lists/standard", json={"name": "Weekly"})
    assert r.json() == {"slug": "standard", "name": "Weekly"}
    assert client.get("/api/lists/standard").json()["name"] == "Weekly"


def test_duplicate_list_copies_items(client):
    client.post("/api/lists/standard/items", json=MILK)
    r = client.post("/api/lists/standard/duplicate", json={"name": "Party"})
    assert r.json() == {"slug": "party"}
    assert [i["upc"] for i in client.get("/api/lists/party").json()["items"]] == ["0001"]


def test_delete_active_list_switches_to_remaining(client):
    client.post("/api/lists", json={"name": "Other"})
    r = client.delete("/api/lists/standard")
    assert r.status_code == 200
    assert r.json() == {"lists": [{"slug": "other", "name": "Other", "count": 0}], "active_list": "other"}


def test_delete_last_list_is_refused(client):
    r = client.delete("/api/lists/standard")
    assert r.status_code == 400
    assert "last" in r.json()["error"].lower()
    assert client.get("/api/state").json()["active_list"] == "standard"


def test_missing_list_is_404_with_error_shape(client):
    assert client.get("/api/lists/ghost").status_code == 404
    assert "error" in client.get("/api/lists/ghost").json()
    assert client.put("/api/active", json={"slug": "ghost"}).status_code == 404
    assert client.patch("/api/lists/ghost", json={"name": "x"}).status_code == 404


def test_get_list_includes_warning_when_file_is_corrupt(client, tmp_path):
    (tmp_path / "lists" / "standard.json").write_text("nope", encoding="utf-8")
    body = client.get("/api/lists/standard").json()
    assert body["items"] == []
    assert "not valid JSON" in body["warning"]


# -- items --------------------------------------------------------------------------

def test_add_item_stores_image_and_dedupes(client):
    r = client.post("/api/lists/standard/items", json={**MILK, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99})
    assert r.json()["items"] == [{**MILK, **FULL, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99}]
    client.post("/api/lists/standard/items", json=MILK)
    assert len(client.get("/api/lists/standard").json()["items"]) == 1


def test_replace_items_saves_quantities_and_flags(client, storage):
    body = [{**MILK, **FULL, "quantity": 3, "included": False}, {**EGGS, **FULL}]
    r = client.put("/api/lists/standard/items", json=body)
    assert r.json()["items"] == body
    assert storage.load_list("standard").items[0].quantity == 3


def test_replace_items_with_empty_body_does_not_touch_corrupt_file(client, tmp_path):
    (tmp_path / "lists" / "standard.json").write_text("nope", encoding="utf-8")
    client.put("/api/lists/standard/items", json=[])
    assert (tmp_path / "lists" / "standard.json").read_text(encoding="utf-8") == "nope"


def test_delete_item_and_missing_item(client):
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)
    assert [i["upc"] for i in client.delete("/api/lists/standard/items/0001").json()["items"]] == ["0002"]
    assert [i["upc"] for i in client.delete("/api/lists/standard/items/9999").json()["items"]] == ["0002"]


# -- push ------------------------------------------------------------------------------

def test_push_sends_only_included_items_with_positive_quantity(client, kroger, storage):
    body = [{**MILK, **FULL, "quantity": 2}, {**EGGS, **FULL, "included": False},
            {"upc": "0003", "name": "Bread", "size": "", **FULL, "quantity": 0}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    assert [i["upc"] for i in r.json()["added"]] == ["0001"]
    assert storage.load_list("standard").items[1].included is False


def test_push_with_nothing_checked_is_400(client, kroger):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL, "included": False}])
    assert r.status_code == 400
    assert kroger.cart_calls == []


def test_push_when_not_signed_in_is_401(client, kroger):
    kroger.cart_error = NotSignedIn()
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.status_code == 401
    assert r.json()["signed_in"] is False


def test_push_kroger_rejection_flags_whole_token_upcs_and_keeps_edits(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0002")
    body = [{**MILK, **FULL}, {**EGGS, **FULL}, {"upc": "02", "name": "Partial", "size": "", **FULL}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0002", "bad_upcs": ["0002"]}
    assert [i.upc for i in storage.load_list("standard").items] == ["0001", "0002", "02"]


# -- prices ------------------------------------------------------------------------------

def test_refresh_prices_requires_store(client):
    assert client.post("/api/lists/standard/prices").status_code == 400


def test_refresh_prices_updates_each_item(client, kroger, with_store):
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    items = r.json()["items"]
    assert kroger.lookup_calls == ["0001", "0002"]
    assert all(i["price"] == 5.0 and i["promo"] == 4.0 and i["price_checked_at"] for i in items)
    assert items[0]["image"] == "https://img/0001.jpg"
    assert r.json()["failed"] == 0


def test_refresh_prices_partial_failure_keeps_old_values(client, kroger, with_store):
    client.post("/api/lists/standard/items", json={**MILK, "price": 1.0})
    client.post("/api/lists/standard/items", json=EGGS)
    kroger.lookups["0001"] = KrogerError(404, "not found")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    milk, eggs = r.json()["items"]
    assert milk["price"] == 1.0 and milk["price_checked_at"] is None
    assert eggs["price"] == 5.0
    assert r.json()["failed"] == 1


def test_refresh_prices_total_failure_is_502(client, kroger, with_store):
    client.post("/api/lists/standard/items", json=MILK)
    kroger.lookups["0001"] = KrogerError(503, "down")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 502
    assert "down" in r.json()["error"]


def test_refresh_prices_on_empty_list_is_noop(client, with_store, kroger):
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    assert kroger.lookup_calls == []


# -- import ------------------------------------------------------------------------------

def test_import_parse_returns_lines(client):
    r = client.post("/api/import/parse", json={"text": "- 2x milk\n- eggs"})
    assert r.json() == {"lines": [
        {"raw": "- 2x milk", "name": "milk", "quantity": 2},
        {"raw": "- eggs", "name": "eggs", "quantity": 1},
    ]}


def test_import_parse_empty_is_400(client):
    assert client.post("/api/import/parse", json={"text": "  "}).status_code == 400


def test_import_resolve_requires_store(client):
    assert client.post("/api/import/resolve", json={"names": ["milk"]}).status_code == 400


def test_import_resolve_returns_up_to_three_candidates_per_name(client, with_store, kroger):
    def boom(term, location_id):
        if term == "unobtainium":
            raise KrogerError(500, "oops")
        return [product(str(i), f"{term} {i}") for i in range(5)]
    kroger.search_products = boom
    r = client.post("/api/import/resolve", json={"names": ["milk", "unobtainium"]})
    matches = r.json()["matches"]
    assert matches[0]["name"] == "milk" and len(matches[0]["candidates"]) == 3
    assert matches[1] == {"name": "unobtainium", "candidates": []}


def test_import_commit_merges_and_bumps_quantities(client):
    client.post("/api/lists/standard/items", json={**MILK, "quantity": 1})
    body = [
        {"upc": "0001", "name": "Whole Milk", "size": "1 gal", "image": "https://img/1.jpg", "quantity": 2},
        {"upc": "0002", "name": "Eggs", "size": "12 ct", "image": None, "quantity": 3},
    ]
    r = client.post("/api/lists/standard/import", json=body)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["upc"], i["quantity"], i["included"]) for i in items] == [("0001", 3, True), ("0002", 3, True)]
    assert items[1]["image"] is None
```

**Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_routes.py -q`
Expected: most tests fail (404s, old shapes). `test_index_and_static_js_are_served` will fail until Task 6 creates `app.js`; that one is allowed to stay red until then.

**Step 3: Replace `app/main.py`**

```python
# app/main.py
"""FastAPI app: serves the page and a JSON API over Storage + KrogerClient."""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .importer import parse as parse_import
from .kroger import KrogerError, NotSignedIn
from .storage import Item, ListLoad, Storage

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESOLVE_CANDIDATES = 3


class StoreChoice(BaseModel):
    id: str
    name: str


class ListName(BaseModel):
    name: str


class ActiveChoice(BaseModel):
    slug: str


class NewItem(BaseModel):
    upc: str
    name: str
    size: str = ""
    image: str | None = None
    price: float | None = None
    promo: float | None = None


class ItemModel(BaseModel):
    upc: str
    name: str
    size: str = ""
    quantity: int = 1
    included: bool = True
    image: str | None = None
    price: float | None = None
    promo: float | None = None
    price_checked_at: str | None = None


class ImportText(BaseModel):
    text: str


class ResolveRequest(BaseModel):
    names: list[str]


class ImportItem(BaseModel):
    upc: str
    name: str
    size: str = ""
    image: str | None = None
    quantity: int = 1


class ListNotFound(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


def create_app(storage: Storage, kroger) -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake)."""
    storage.ensure_ready()
    app = FastAPI(title="Kroger Reorder")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def error(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": message, **extra})

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(_: Request, exc: NotSignedIn):
        return error(401, exc.message, signed_in=False)

    def kroger_message(exc: KrogerError) -> str:
        # status 0 is a transport failure: Kroger never answered, so there is nothing to quote.
        return exc.message if exc.status == 0 else f"Kroger said: {exc.message}"

    @app.exception_handler(KrogerError)
    async def kroger_error(_: Request, exc: KrogerError):
        return error(502, kroger_message(exc))

    @app.exception_handler(ListNotFound)
    async def list_not_found(_: Request, exc: ListNotFound):
        return error(404, f"No list named '{exc.slug}'.")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error(422, f"Invalid request: {loc or 'body'} {first.get('msg', '')}".strip())

    # -- helpers ----------------------------------------------------------------------
    def get_list(slug: str) -> ListLoad:
        loaded = storage.load_list(slug)
        if loaded is None:
            raise ListNotFound(slug)
        return loaded

    def list_payload(loaded: ListLoad, **extra) -> dict:
        return {"slug": loaded.slug, "name": loaded.name,
                "items": [asdict(i) for i in loaded.items], "warning": loaded.warning, **extra}

    def lists_payload() -> dict:
        return {"lists": storage.list_lists(), "active_list": storage.active_list()}

    def store_id() -> str | None:
        return storage.load_config().get("store_id")

    def clean_name(raw: str) -> str | None:
        name = raw.strip()
        return name or None

    # -- page / setup -----------------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        config = storage.load_config()
        store = {"store_id": config["store_id"], "store_name": config.get("store_name", "")} if config.get("store_id") else None
        return {"store": store, "signed_in": kroger.is_signed_in(), **lists_payload()}

    @app.get("/api/stores")
    def stores(zip: str):
        return {"stores": kroger.find_stores(zip)}

    @app.put("/api/store")
    def choose_store(choice: StoreChoice):
        storage.save_config({**storage.load_config(), "store_id": choice.id, "store_name": choice.name})
        return {"store": {"store_id": choice.id, "store_name": choice.name}}

    @app.get("/auth/login")
    def login():
        return RedirectResponse(kroger.authorize_url())

    @app.get("/callback")
    def callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error or not code:
            return RedirectResponse(f"/?auth_error={quote(error or 'no code returned')}")
        try:
            kroger.exchange_code(code, state)
        except KrogerError as exc:
            return RedirectResponse(f"/?auth_error={quote(exc.message)}")
        return RedirectResponse("/")

    @app.get("/api/search")
    def search(term: str):
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        return {"results": kroger.search_products(term, sid)}

    # -- lists ------------------------------------------------------------------------------
    @app.post("/api/lists")
    def create_list(body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the list a name.")
        return {"slug": storage.create_list(name)}

    @app.patch("/api/lists/{slug}")
    def rename_list(slug: str, body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the list a name.")
        get_list(slug)
        storage.rename_list(slug, name)
        return {"slug": slug, "name": name}

    @app.post("/api/lists/{slug}/duplicate")
    def duplicate_list(slug: str, body: ListName):
        name = clean_name(body.name)
        if not name:
            return error(400, "Give the copy a name.")
        get_list(slug)
        return {"slug": storage.duplicate_list(slug, name)}

    @app.delete("/api/lists/{slug}")
    def delete_list(slug: str):
        get_list(slug)
        if len(storage.list_slugs()) <= 1:
            return error(400, "You can't delete the last list. Create another one first.")
        storage.delete_list(slug)
        return lists_payload()

    @app.put("/api/active")
    def set_active(body: ActiveChoice):
        get_list(body.slug)
        storage.set_active_list(body.slug)
        return {"active_list": body.slug}

    @app.get("/api/lists/{slug}")
    def read_list(slug: str):
        return list_payload(get_list(slug))

    # -- items ----------------------------------------------------------------------------
    @app.post("/api/lists/{slug}/items")
    def add_item(slug: str, new: NewItem):
        loaded = get_list(slug)
        if not any(i.upc == new.upc for i in loaded.items):
            loaded.items.append(Item(**new.model_dump()))
            storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug))

    @app.put("/api/lists/{slug}/items")
    def replace_items(slug: str, models: list[ItemModel]):
        loaded = get_list(slug)
        if models:  # an empty body is a no-op, never a clear; removal goes through DELETE
            storage.save_list(slug, loaded.name, [Item(**m.model_dump()) for m in models])
        return list_payload(get_list(slug))

    @app.delete("/api/lists/{slug}/items/{upc}")
    def remove_item(slug: str, upc: str):
        loaded = get_list(slug)
        kept = [i for i in loaded.items if i.upc != upc]
        if len(kept) != len(loaded.items):
            storage.save_list(slug, loaded.name, kept)
        return list_payload(get_list(slug))

    @app.post("/api/lists/{slug}/prices")
    def refresh_prices(slug: str):
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        loaded = get_list(slug)
        if not loaded.items:
            return list_payload(loaded, failed=0)
        failed, last_error = 0, None
        now = datetime.now().isoformat(timespec="seconds")
        for item in loaded.items:
            try:
                fresh = kroger.lookup_product(item.upc, sid)
            except KrogerError as exc:
                failed += 1
                last_error = exc
                continue
            item.price, item.promo, item.price_checked_at = fresh.get("price"), fresh.get("promo"), now
            item.image = fresh.get("image") or item.image
        if failed == len(loaded.items):
            return error(502, f"Couldn't check prices: {kroger_message(last_error)}")
        storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug), failed=failed)

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
            # Whole-token match so "1111041" is not flagged when Kroger rejects "0001111041700".
            bad = [i.upc for i in chosen if re.search(rf"(?<!\d){re.escape(i.upc)}(?!\d)", exc.message)]
            return error(502, kroger_message(exc), bad_upcs=bad)
        return {"added": [asdict(i) for i in chosen]}

    # -- import ----------------------------------------------------------------------------
    @app.post("/api/import/parse")
    def import_parse(body: ImportText):
        lines = parse_import(body.text)
        if not lines:
            return error(400, "Couldn't find any items in that text. Try one item per line.")
        return {"lines": [asdict(line) for line in lines]}

    @app.post("/api/import/resolve")
    def import_resolve(body: ResolveRequest):
        sid = store_id()
        if not sid:
            return error(400, "Pick a store first.")
        matches = []
        for name in body.names:
            try:
                candidates = kroger.search_products(name, sid)[:RESOLVE_CANDIDATES]
            except KrogerError:
                candidates = []
            matches.append({"name": name, "candidates": candidates})
        return {"matches": matches}

    @app.post("/api/lists/{slug}/import")
    def import_commit(slug: str, picks: list[ImportItem]):
        loaded = get_list(slug)
        by_upc = {i.upc: i for i in loaded.items}
        for pick in picks:
            if pick.upc in by_upc:
                by_upc[pick.upc].quantity += max(1, pick.quantity)
            else:
                item = Item(upc=pick.upc, name=pick.name, size=pick.size, image=pick.image, quantity=max(1, pick.quantity))
                loaded.items.append(item)
                by_upc[pick.upc] = item
        if picks:
            storage.save_list(slug, loaded.name, loaded.items)
        return list_payload(get_list(slug))

    return app
```

**Step 4: Create a placeholder `app/static/app.js`** so the static test passes now (Task 6 replaces it):

```bash
printf '// replaced in Task 6\n' > app/static/app.js
```

**Step 5: Run everything**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `25 + 29 + 13 + 4 + 40 = 111 passed` (storage, kroger, importer, settings, routes). If the count differs slightly, make sure every file is green rather than chasing the number.

**Step 6: Commit**

```bash
git add app/main.py app/static/app.js tests/test_routes.py
git commit -m "feat(api): list CRUD, scoped items and push, price refresh, import endpoints

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Entry point — LAN binding, startup migration

**Files:**
- Replace: `app/__main__.py`

No new automated tests (network-bound). Verified by the manual start in Step 2.

**Step 1: Replace the file**

```python
# app/__main__.py
"""`python -m app` (or `kroger-reorder`): start the server for the whole LAN and open the browser."""
from __future__ import annotations

import socket
import sys
import threading
import webbrowser

import uvicorn

from .kroger import KrogerClient
from .main import create_app
from .settings import load_settings
from .storage import Storage


def lan_ip() -> str | None:
    """Best-effort LAN address: the source IP the OS would use to reach the internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packets are sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def main() -> None:
    settings = load_settings()
    storage = Storage()
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    app = create_app(storage, kroger)  # also migrates v1 data and ensures a default list
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", settings.port))
        except OSError:
            sys.exit(
                f"Port {settings.port} is already in use (is Kroger Reorder already running?). "
                "Close it, or set PORT in .env and update the redirect URL on your Kroger app."
            )
    local = f"http://localhost:{settings.port}"
    print(f"Kroger Reorder running at {local}", flush=True)
    if ip := lan_ip():
        print(f"  On your wifi:  http://{ip}:{settings.port}", flush=True)
    print("  Sign in to Kroger from this computer; other devices share the sign-in.  (Ctrl+C to stop)", flush=True)
    threading.Timer(1.0, webbrowser.open, args=(local,)).start()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
```

**Step 2: Verify it starts and migrates**

Make a throwaway v1 file first, then start with fake credentials on a spare port:

```bash
mkdir -p data && printf '[{"upc":"0001","name":"Milk","size":"1 gal"}]' > data/list.json
KROGER_CLIENT_ID=x KROGER_CLIENT_SECRET=y PORT=8767 timeout 6 .venv/Scripts/python -m app & sleep 3
curl -s http://127.0.0.1:8767/api/state; echo
curl -s http://127.0.0.1:8767/api/lists/standard; echo
wait; ls data data/lists; rm -rf data
```

Expected: the state shows `"lists":[{"slug":"standard","name":"Standard","count":1}]`, the list has Milk, `data/list.json.migrated` exists, and the console printed both URLs. (Your real `.env` is read by `load_dotenv` but the explicit env vars take precedence; the test never calls Kroger.)

**Step 3: Commit**

```bash
git add app/__main__.py
git commit -m "feat: bind to the LAN and print the wifi URL at startup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: The page — list selector, thumbnails, badges, import dialog

**Files:**
- Replace: `app/static/index.html`
- Replace: `app/static/app.js`

**Step 1: Write `app/static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kroger Reorder</title>
<style>
  :root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --accent:#0b5fff; --danger:#b3261e; --ok:#1b7f3b; --line:#e2e2de; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--ink); }
  main { max-width:760px; margin:0 auto; padding:24px 16px 96px; }
  h1 { font-size:1.5rem; margin:0 0 16px; }
  h2 { font-size:1.05rem; margin:0 0 8px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .muted { color:var(--muted); font-size:.9rem; }
  .banner { padding:10px 12px; border-radius:8px; margin-bottom:16px; display:none; }
  .banner.error { display:block; background:#fde8e6; color:var(--danger); }
  .banner.ok { display:block; background:#e4f5ea; color:var(--ok); }
  .banner.warn { display:block; background:#fff4d6; color:#7a5300; }
  input[type=text], input[type=number], select, textarea { font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); }
  input[type=number] { width:64px; }
  textarea { width:100%; margin:8px 0; resize:vertical; }
  button { font:inherit; padding:8px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  button.secondary { background:#eceeea; color:var(--ink); }
  button.danger { background:transparent; color:var(--danger); padding:4px 8px; }
  button:disabled { opacity:.5; cursor:default; }
  .btn { display:inline-block; font:inherit; padding:8px 14px; border-radius:8px; background:var(--accent); color:#fff; text-decoration:none; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .row.between { justify-content:space-between; }
  ul { list-style:none; margin:8px 0 0; padding:0; }
  li { display:flex; gap:10px; align-items:center; padding:10px 0; border-top:1px solid var(--line); }
  li:first-child { border-top:0; }
  li .name { flex:1; min-width:0; }
  li .size { color:var(--muted); font-size:.85rem; }
  li.bad { background:#fde8e6; margin:0 -8px; padding:10px 8px; border-radius:8px; }
  .results li { cursor:pointer; }
  .results li:hover { background:#f2f4f0; }
  .thumb { width:56px; height:56px; object-fit:contain; border-radius:6px; background:#fff; border:1px solid var(--line); flex:none; }
  .thumb.placeholder { background:#f2f4f0; }
  .results .thumb { width:40px; height:40px; }
  .price { color:var(--muted); font-size:.9rem; white-space:nowrap; }
  .sale { color:var(--ok); font-weight:600; font-size:.9rem; white-space:nowrap; }
  .sale s { color:var(--muted); font-weight:400; margin-left:4px; }
  .menu-wrap { position:relative; }
  .menu { position:absolute; right:0; top:42px; background:var(--card); border:1px solid var(--line); border-radius:8px; box-shadow:0 4px 16px rgba(0,0,0,.08); display:flex; flex-direction:column; min-width:170px; z-index:5; overflow:hidden; }
  .menu button { background:transparent; color:var(--ink); text-align:left; border-radius:0; padding:10px 14px; }
  .menu button:hover { background:#f2f4f0; }
  .menu button.danger { color:var(--danger); }
  dialog { border:1px solid var(--line); border-radius:12px; padding:20px; width:min(760px, 95vw); color:var(--ink); }
  dialog::backdrop { background:rgba(0,0,0,.3); }
  table { width:100%; border-collapse:collapse; margin:12px 0; font-size:.95rem; }
  th, td { text-align:left; padding:6px 4px; border-top:1px solid var(--line); vertical-align:middle; }
  th { border-top:0; color:var(--muted); font-weight:500; }
  td select { max-width:380px; width:100%; }
  td .raw { color:var(--muted); font-size:.85rem; }
  footer { position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:12px 16px; }
  footer .inner { max-width:760px; margin:0 auto; display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
  .hidden { display:none !important; }
</style>
</head>
<body>
<main>
  <h1>Kroger Reorder</h1>
  <div id="banner" class="banner"></div>

  <section id="setup">
    <h2>Setup</h2>
    <div id="store-block">
      <div class="row">
        <input id="zip" type="text" inputmode="numeric" placeholder="Zip code" maxlength="5">
        <button id="find-stores">Find stores</button>
      </div>
      <ul id="store-results" class="results"></ul>
    </div>
    <div id="signin-block" class="row" style="margin-top:12px">
      <span class="muted">Sign in once so the app can add to your cart.</span>
      <a id="signin-link" class="btn" href="/auth/login">Sign in to Kroger</a>
      <span id="signin-remote" class="muted hidden">Sign in from the computer running the app; this device will share it.</span>
    </div>
  </section>

  <section id="add">
    <h2>Add an item</h2>
    <div class="row">
      <input id="term" type="text" placeholder="Search your store, e.g. whole milk" style="flex:1">
      <button id="search">Search</button>
    </div>
    <ul id="search-results" class="results"></ul>
  </section>

  <section id="list-section">
    <div class="row between">
      <div class="row">
        <select id="list-select" title="Choose a list"></select>
        <div class="menu-wrap">
          <button id="menu-btn" class="secondary" title="List actions">&#x22EF;</button>
          <div id="menu" class="menu hidden">
            <button data-action="new">New list</button>
            <button data-action="rename">Rename</button>
            <button data-action="duplicate">Duplicate</button>
            <button data-action="import">Import items</button>
            <button data-action="delete" class="danger">Delete list</button>
          </div>
        </div>
      </div>
      <div class="row">
        <span id="store-label" class="muted"></span>
        <button id="change-store" class="secondary">Change store</button>
      </div>
    </div>
    <div class="row between" style="margin-top:10px">
      <span id="price-status" class="muted"></span>
      <button id="check-prices" class="secondary">Check prices</button>
    </div>
    <ul id="items"></ul>
    <p id="empty" class="muted hidden">Nothing in this list yet. Search above to add items, or import a list from the menu.</p>
  </section>
</main>

<footer>
  <div class="inner">
    <span class="muted">Start from an empty Kroger cart, or quantities will double.</span>
    <button id="push">Send to cart</button>
  </div>
</footer>

<dialog id="import-dialog">
  <h2>Import items</h2>
  <p class="muted">CSV, JSON, Markdown, or plain text, one item per line. Quantities like <code>2x milk</code> or <code>milk x2</code> are understood.</p>
  <div class="row">
    <input id="import-file" type="file" accept=".csv,.json,.md,.txt,text/*,application/json">
  </div>
  <textarea id="import-text" rows="6" placeholder="...or paste your list here"></textarea>
  <div id="import-banner" class="banner"></div>
  <div class="row">
    <button id="import-parse">Find matches</button>
    <button id="import-close" class="secondary">Cancel</button>
  </div>
  <div id="import-review" class="hidden">
    <table id="import-table">
      <thead><tr><th>Your line</th><th>Qty</th><th>Match at your store</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="row">
      <button id="import-commit">Add to list</button>
      <span id="import-summary" class="muted"></span>
    </div>
  </div>
</dialog>

<script src="/static/app.js"></script>
</body>
</html>
```

**Step 2: Write `app/static/app.js`**

```js
// Kroger Reorder page logic. Vanilla JS, talks to the JSON API in app/main.py.
const $ = (id) => document.getElementById(id);
const IS_HOST = ['localhost', '127.0.0.1'].includes(location.hostname);
const STALE_MS = 24 * 60 * 60 * 1000;

let state = { store: null, signed_in: false, lists: [], active_list: null, name: '', items: [], warning: null };
let importRows = [];  // [{raw, name, quantity, candidates}]

// -- helpers ----------------------------------------------------------------------------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.signed_in === false) { state.signed_in = false; render(); }
    throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { data });
  }
  return data;
}

function banner(kind, text, html = false, el = $('banner')) {
  el.className = 'banner' + (kind ? ' ' + kind : '');
  if (html) el.innerHTML = text || ''; else el.textContent = text || '';
}

function money(n) { return `$${Number(n).toFixed(2)}`; }

function priceHtml(p) {
  if (p.promo != null && p.price != null && Number(p.promo) < Number(p.price)) {
    return `<span class="sale">${money(p.promo)}<s>${money(p.price)}</s></span>`;
  }
  if (p.promo != null && p.price == null) return `<span class="sale">${money(p.promo)}</span>`;
  if (p.price != null) return `<span class="price">${money(p.price)}</span>`;
  return '';
}

function thumb(url) {
  return url ? `<img class="thumb" src="${esc(url)}" alt="" loading="lazy">` : `<div class="thumb placeholder"></div>`;
}

function listPath(suffix = '') {
  return `/api/lists/${encodeURIComponent(state.active_list)}${suffix}`;
}

function cleanItems() {
  return state.items.map(({ upc, name, size, quantity, included, image, price, promo, price_checked_at }) =>
    ({ upc, name, size, quantity, included, image, price, promo, price_checked_at }));
}

function ago(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3600000);
  if (h < 1) return 'just now';
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.floor(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

function newestCheck() {
  const stamps = state.items.map(i => i.price_checked_at).filter(Boolean).sort();
  return stamps.length ? stamps[stamps.length - 1] : null;
}

function pricesStale() {
  if (!state.store || !state.items.length) return false;
  const newest = newestCheck();
  return !newest || (Date.now() - new Date(newest).getTime()) > STALE_MS;
}

// -- rendering ------------------------------------------------------------------------

function render() {
  const setupDone = !!(state.store && state.signed_in);
  $('setup').classList.toggle('hidden', setupDone);
  $('store-block').classList.toggle('hidden', !!state.store);
  $('signin-block').classList.toggle('hidden', !!state.signed_in);
  $('signin-link').classList.toggle('hidden', !IS_HOST);
  $('signin-remote').classList.toggle('hidden', IS_HOST);
  $('add').classList.toggle('hidden', !state.store);
  $('store-label').textContent = state.store ? state.store.store_name : '';
  $('push').disabled = !setupDone || !state.items.some(i => i.included);
  $('check-prices').disabled = !state.store || !state.items.length;

  const sel = $('list-select');
  sel.innerHTML = '';
  state.lists.forEach(l => {
    const opt = document.createElement('option');
    opt.value = l.slug;
    opt.textContent = `${l.name} (${l.count})`;
    opt.selected = l.slug === state.active_list;
    sel.appendChild(opt);
  });

  const newest = newestCheck();
  $('price-status').textContent = !state.items.length ? '' : newest ? `Prices checked ${ago(newest)}` : 'Prices not checked yet';

  const ul = $('items');
  const active = document.activeElement?.dataset || {};
  ul.innerHTML = '';
  state.items.forEach((item, idx) => {
    const li = document.createElement('li');
    if (item.bad) li.classList.add('bad');
    li.innerHTML = `
      <input type="checkbox" ${item.included ? 'checked' : ''} data-idx="${idx}" data-field="included">
      ${thumb(item.image)}
      <div class="name"><div>${esc(item.name)}</div><div class="size">${esc(item.size)} ${priceHtml(item)}</div></div>
      <input type="number" min="1" value="${item.quantity}" data-idx="${idx}" data-field="quantity">
      <button class="danger" data-remove="${esc(item.upc)}" title="Remove">&#x2715;</button>`;
    ul.appendChild(li);
  });
  if (active.idx !== undefined) {
    ul.querySelector(`[data-idx="${active.idx}"][data-field="${active.field}"]`)?.focus();
  }
  $('empty').classList.toggle('hidden', state.items.length > 0);
}

function adoptList(data) {
  const bad = new Set(state.items.filter(i => i.bad).map(i => i.upc));
  state.items = data.items.map(i => ({ ...i, bad: bad.has(i.upc) }));
  state.name = data.name;
  state.warning = data.warning;
  const entry = state.lists.find(l => l.slug === data.slug);
  if (entry) { entry.name = data.name; entry.count = data.items.length; }
}

// -- loading ----------------------------------------------------------------------------

async function refreshLists() {
  const s = await api('GET', '/api/state');
  Object.assign(state, { store: s.store, signed_in: s.signed_in, lists: s.lists, active_list: s.active_list });
}

async function loadList() {
  if (!state.active_list) { state.items = []; state.name = ''; return; }
  adoptList(await api('GET', listPath()));
  if (state.warning) banner('warn', state.warning);
}

async function refreshPrices(manual) {
  if (!state.store || !state.items.length) return;
  $('check-prices').disabled = true;
  $('price-status').textContent = 'Checking prices…';
  try {
    const data = await api('POST', listPath('/prices'));
    adoptList(data);
    if (manual) banner('ok', data.failed ? `Prices updated; ${data.failed} item${data.failed === 1 ? '' : 's'} couldn't be checked.` : 'Prices updated.');
  } catch (e) {
    if (manual) banner('error', e.message);
  }
  render();
}

async function load() {
  const params = new URLSearchParams(location.search);
  const authError = params.get('auth_error');
  if (authError) history.replaceState(null, '', location.pathname);
  try {
    await refreshLists();
    await loadList();
    render();
    if (authError) banner('error', `Kroger sign-in failed: ${authError}`);
    if (pricesStale()) refreshPrices(false);
  } catch (e) { banner('error', e.message); render(); }
}

async function switchList(slug) {
  await api('PUT', '/api/active', { slug });
  state.active_list = slug;
  banner();
  await loadList();
  render();
  if (pricesStale()) refreshPrices(false);
}

async function saveItems() {
  try { adoptList(await api('PUT', listPath('/items'), cleanItems())); }
  catch (e) { banner('error', e.message); }
}

// -- setup ------------------------------------------------------------------------------

$('find-stores').onclick = async () => {
  banner();
  try {
    const { stores } = await api('GET', `/api/stores?zip=${encodeURIComponent($('zip').value.trim())}`);
    const ul = $('store-results'); ul.innerHTML = '';
    stores.forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = `<div class="name"><div>${esc(s.name)}</div><div class="size">${esc(s.address)}</div></div>`;
      li.onclick = async () => {
        try {
          const data = await api('PUT', '/api/store', { id: s.id, name: s.name });
          state.store = data.store; ul.innerHTML = ''; render();
          if (pricesStale()) refreshPrices(false);
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!stores.length) banner('warn', 'No stores found near that zip.');
  } catch (e) { banner('error', e.message); }
};

$('change-store').onclick = () => { state.store = null; render(); };

// -- search / add -----------------------------------------------------------------------

async function search() {
  banner();
  const term = $('term').value.trim();
  if (!term) return;
  try {
    const { results } = await api('GET', `/api/search?term=${encodeURIComponent(term)}`);
    const ul = $('search-results'); ul.innerHTML = '';
    results.forEach(r => {
      const li = document.createElement('li');
      li.innerHTML = `${thumb(r.image)}<div class="name"><div>${esc(r.name)}</div><div class="size">${esc(r.size)}</div></div>${priceHtml(r)}`;
      li.onclick = async () => {
        try {
          const data = await api('POST', listPath('/items'),
            { upc: r.upc, name: r.name, size: r.size, image: r.image, price: r.price, promo: r.promo });
          adoptList(data); ul.innerHTML = ''; $('term').value = ''; render();
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!results.length) banner('warn', `No matches for "${term}" at your store.`);
  } catch (e) { banner('error', e.message); }
}
$('search').onclick = search;
$('term').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });

// -- list rows ----------------------------------------------------------------------------

$('items').addEventListener('change', async (e) => {
  const { idx, field } = e.target.dataset;
  if (idx === undefined) return;
  const item = state.items[idx];
  if (field === 'included') item.included = e.target.checked;
  if (field === 'quantity') item.quantity = Math.max(1, parseInt(e.target.value || '1', 10) || 1);
  await saveItems();
  render();
});

$('items').addEventListener('click', async (e) => {
  const upc = e.target.dataset.remove;
  if (!upc) return;
  try { adoptList(await api('DELETE', listPath(`/items/${encodeURIComponent(upc)}`))); render(); }
  catch (err) { banner('error', err.message); }
});

$('check-prices').onclick = () => { banner(); refreshPrices(true); };

// -- list selector and menu --------------------------------------------------------------

$('list-select').onchange = (e) => switchList(e.target.value).catch(err => banner('error', err.message));

$('menu-btn').onclick = (e) => { e.stopPropagation(); $('menu').classList.toggle('hidden'); };
document.addEventListener('click', () => $('menu').classList.add('hidden'));

$('menu').addEventListener('click', async (e) => {
  const action = e.target.dataset.action;
  if (!action) return;
  $('menu').classList.add('hidden');
  banner();
  try {
    if (action === 'new') {
      const name = prompt('Name for the new list:');
      if (!name?.trim()) return;
      const { slug } = await api('POST', '/api/lists', { name });
      await refreshLists();
      await switchList(slug);
    } else if (action === 'rename') {
      const name = prompt('New name for this list:', state.name);
      if (!name?.trim() || name === state.name) return;
      await api('PATCH', listPath(), { name });
      await refreshLists(); await loadList(); render();
    } else if (action === 'duplicate') {
      const name = prompt('Name for the copy:', `${state.name} copy`);
      if (!name?.trim()) return;
      const { slug } = await api('POST', listPath('/duplicate'), { name });
      await refreshLists();
      await switchList(slug);
    } else if (action === 'delete') {
      if (!confirm(`Delete "${state.name}" and its ${state.items.length} item${state.items.length === 1 ? '' : 's'}?`)) return;
      const data = await api('DELETE', listPath());
      state.lists = data.lists; state.active_list = data.active_list;
      await loadList(); render();
    } else if (action === 'import') {
      openImport();
    }
  } catch (err) { banner('error', err.message); }
});

// -- import dialog --------------------------------------------------------------------------

function openImport() {
  importRows = [];
  $('import-text').value = '';
  $('import-file').value = '';
  $('import-review').classList.add('hidden');
  $('import-table').querySelector('tbody').innerHTML = '';
  banner(null, '', false, $('import-banner'));
  $('import-dialog').showModal();
}

$('import-close').onclick = () => $('import-dialog').close();

$('import-file').onchange = async (e) => {
  const file = e.target.files[0];
  if (file) $('import-text').value = await file.text();
};

$('import-parse').onclick = async () => {
  banner(null, '', false, $('import-banner'));
  const text = $('import-text').value;
  try {
    const { lines } = await api('POST', '/api/import/parse', { text });
    $('import-parse').disabled = true;
    banner('warn', `Looking up ${lines.length} item${lines.length === 1 ? '' : 's'} at your store…`, false, $('import-banner'));
    const { matches } = await api('POST', '/api/import/resolve', { names: lines.map(l => l.name) });
    importRows = lines.map((l, i) => ({ ...l, candidates: matches[i].candidates }));
    banner(null, '', false, $('import-banner'));
    renderImport();
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  }
  $('import-parse').disabled = false;
};

function renderImport() {
  const tbody = $('import-table').querySelector('tbody');
  tbody.innerHTML = '';
  importRows.forEach((row, idx) => {
    const tr = document.createElement('tr');
    const options = row.candidates.map((c, ci) =>
      `<option value="${ci}" ${ci === 0 ? 'selected' : ''}>${esc(c.name)} · ${esc(c.size)}${c.price != null ? ' · ' + money(c.promo ?? c.price) : ''}</option>`
    ).join('');
    tr.innerHTML = `
      <td><div>${esc(row.name)}</div><div class="raw">${esc(row.raw)}</div></td>
      <td><input type="number" min="1" value="${row.quantity}" data-import-qty="${idx}"></td>
      <td><select data-import-pick="${idx}">${options}<option value="-1" ${row.candidates.length ? '' : 'selected'}>${row.candidates.length ? 'Skip this line' : 'No match found — skip'}</option></select></td>`;
    tbody.appendChild(tr);
  });
  $('import-review').classList.remove('hidden');
  updateImportSummary();
}

function importPicks() {
  return importRows.map((row, idx) => {
    const pick = parseInt($('import-table').querySelector(`[data-import-pick="${idx}"]`).value, 10);
    const qty = Math.max(1, parseInt($('import-table').querySelector(`[data-import-qty="${idx}"]`).value || '1', 10) || 1);
    if (pick < 0) return null;
    const c = row.candidates[pick];
    return { upc: c.upc, name: c.name, size: c.size, image: c.image, quantity: qty };
  }).filter(Boolean);
}

function updateImportSummary() {
  const n = importPicks().length;
  $('import-commit').disabled = n === 0;
  $('import-summary').textContent = `${n} of ${importRows.length} line${importRows.length === 1 ? '' : 's'} will be added`;
}

$('import-table').addEventListener('change', updateImportSummary);

$('import-commit').onclick = async () => {
  const picks = importPicks();
  try {
    adoptList(await api('POST', listPath('/import'), picks));
    $('import-dialog').close();
    render();
    banner('ok', `Added ${picks.length} item${picks.length === 1 ? '' : 's'} to ${state.name}.`);
    if (pricesStale()) refreshPrices(false);
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  }
};

// -- push -------------------------------------------------------------------------------------

$('push').onclick = async () => {
  banner();
  $('push').disabled = true;
  try {
    const { added } = await api('POST', listPath('/push'), cleanItems());
    state.items.forEach(i => delete i.bad);
    banner('ok', `Added to your Kroger cart: ${added.map(i => esc(i.name)).join(', ')}. ` +
      `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`, true);
  } catch (e) {
    const bad = (e.data && e.data.bad_upcs) || [];
    state.items.forEach(i => { i.bad = bad.includes(i.upc); });
    banner('error', e.message + (bad.length ? ' The highlighted item was rejected; remove or replace it.' : ''));
  }
  render();
};

load();
```

**Step 3: Syntax-check the JS and serve-check the page**

```bash
node --check app/static/app.js && echo JS OK
.venv/Scripts/python -m pytest -q
```

Expected: `JS OK` (skip if node is unavailable and say so) and the full suite green. Then write a throwaway script in the scratchpad that uses `TestClient` to GET `/` and `/static/app.js`, asserting the HTML contains `id="list-select"`, `id="import-dialog"`, and `/static/app.js`, and the JS contains `listPath(` and `refreshPrices(`. Run it, delete it.

**Step 4: Commit**

```bash
git add app/static/index.html app/static/app.js
git commit -m "feat(ui): list selector and menu, thumbnails, sale badges, price check, import dialog

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: README and manual smoke test

**Files:**
- Modify: `README.md`

**Step 1: Update README**

Make these edits (keep everything else):

1. Intro paragraph: after the first sentence add "Keep several named lists, see product photos and sale prices, and import a list from a file."
2. "### 3. Run" section: after the sentence about the browser opening, add:
   > The console also prints a wifi address like `http://192.168.1.20:8000`. Open that on your phone or any device on the same network. Sign in to Kroger from this computer; every device shares the sign-in.
3. "## Everyday use": renumber and insert as step 3: "Pick a list from the dropdown. The ⋯ menu creates, renames, duplicates, deletes, or imports."
4. New section after Everyday use:
   ```markdown
   ## Sale prices

   Items show a green sale price with the regular price struck through when your store has a promo. Prices are checked in the background when you open a list that hasn't been checked in a day, or any time with **Check prices**. These are shelf promos from Kroger's catalog, not digital coupons; coupons still have to be clipped in the Kroger app.

   ## Importing a list

   From the ⋯ menu choose **Import items**, then pick a file or paste text. Accepted shapes:

   - Plain text or Markdown: one item per line. `- [ ] 2x milk`, `eggs x3`, and `bread (4)` all work.
   - CSV: a `name` column and optional `quantity` column, or just two columns.
   - JSON: an array of strings or of `{"name": ..., "quantity": ...}` objects.

   Each line is matched against your store's catalog. Pick a different match or skip a line in the review table, then **Add to list**. Items already in the list get their quantity bumped.
   ```
5. "## Where your data lives": replace the `list.json` bullet with:
   - `lists/<name>.json` — one file per list. Safe to edit by hand; an unreadable or malformed file is shown as a warning and moved aside as `<name>.json.corrupt-<timestamp>` the next time you add an item.
   - After upgrading from v1, your old `list.json` becomes `lists/standard.json` and the original is kept as `list.json.migrated`.
6. Troubleshooting, add:
   - **Can't sign in from my phone** — expected. Sign in once from the computer running the app.
   - **Can't delete a list** — you can't delete the last one. Create another first.
7. "### Manual smoke test" append steps 5–8:
   5. Open the wifi URL on a phone. Images and the list should appear; the sign-in button is replaced by a note.
   6. Search an item that's on sale at your store; the result and the list row show a green badge.
   7. Import a five-line text file via the ⋯ menu; confirm matches; the items appear.
   8. Create a second list, switch between them, delete one.

**Step 2: Run the full suite one last time**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all green (around 111).

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README for lists, sale prices, LAN access, and import

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

**Step 4: Hand off**

Do not merge or push; the coordinator reviews, merges, and asks the user to run the manual smoke test. Report the final test count and `git log --oneline main..HEAD`.

---

## Done criteria

- Full suite green with the new storage, kroger, importer, and route tests.
- `python -m app` prints both URLs; a v1 `data/list.json` is migrated to `data/lists/standard.json` on first start.
- Manual: phone on wifi sees the list with images; a sale item shows a badge; a five-line text import lands in the list; a second list can be created, switched to, and deleted.
