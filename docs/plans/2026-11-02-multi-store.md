# Lists for other stores (v7) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A list can belong to Aldi, Meijer or Walmart; such a list holds plain items and ends with an "Open at <store>" panel instead of Send to cart. Kroger lists are unchanged.

**Architecture:** A tiny provider table (`stores.py`) describes each store. Storage persists a `store` per list. Routes branch on `provider.has_catalog`: Kroger keeps UPCs, catalog search, prices and cart; hand-off stores key items by a name slug and expose `GET /api/lists/{slug}/handoff`. The page hides catalog features on a hand-off list and renders the panel.

**Tech Stack:** FastAPI, pydantic v2, pytest, vanilla JS. No new dependencies.

Design: `docs/plans/2026-11-02-multi-store-design.md`.

---

### Task 1: Provider table

**Files:** Create `kroger_reorder/stores.py`, `tests/test_stores.py`.

**Step 1: Failing tests**

```python
# tests/test_stores.py
import pytest
from kroger_reorder.stores import (DEFAULT_STORE, PROVIDERS, UnknownStore, item_key, provider,
                                   search_link, store_names)


def test_kroger_is_the_default_and_the_only_catalog_store():
    assert DEFAULT_STORE == "kroger"
    assert [k for k, p in PROVIDERS.items() if p.has_catalog] == ["kroger"]
    assert list(PROVIDERS) == ["kroger", "aldi", "meijer", "walmart"]


def test_hand_off_stores_have_https_search_and_home_links():
    for key in ("aldi", "meijer", "walmart"):
        p = provider(key)
        assert p.search_url.startswith("https://") and "{term}" in p.search_url
        assert p.home_url.startswith("https://")
    assert provider("kroger").search_url is None


def test_search_link_encodes_the_term():
    assert search_link("walmart", "2% milk & eggs") == "https://www.walmart.com/search?q=2%25%20milk%20%26%20eggs"


def test_unknown_store_raises():
    with pytest.raises(UnknownStore) as exc:
        provider("costco")
    assert exc.value.key == "costco"
    assert "Kroger, Aldi, Meijer, or Walmart" in exc.value.message


def test_item_key_is_a_name_slug():
    assert item_key("  Bananas! ") == "bananas"
    assert item_key("???") == "item"


def test_store_names_for_messages():
    assert store_names() == "Kroger, Aldi, Meijer, or Walmart"
```

**Step 2:** `pytest tests/test_stores.py -q` → ImportError.

**Step 3: Implementation**

```python
# kroger_reorder/stores.py
"""The stores a list can belong to. Kroger has a catalog and a cart API; the others are hand-off
stores: the app keeps plain items and opens the store's own site to shop them."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .storage import slugify


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    has_catalog: bool               # True: UPCs, catalog search, prices, Send to cart (Kroger only)
    search_url: str | None = None   # "{term}" is replaced by the URL-encoded search term
    home_url: str | None = None


PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    Provider("kroger", "Kroger", True),
    Provider("aldi", "Aldi", False, "https://www.aldi.us/store/aldi/s?k={term}", "https://www.aldi.us/"),
    Provider("meijer", "Meijer", False, "https://www.meijer.com/shopping/search.html?text={term}", "https://www.meijer.com/"),
    Provider("walmart", "Walmart", False, "https://www.walmart.com/search?q={term}", "https://www.walmart.com/"),
)}
DEFAULT_STORE = "kroger"


def store_names() -> str:
    names = [p.name for p in PROVIDERS.values()]
    return ", ".join(names[:-1]) + f", or {names[-1]}"


class UnknownStore(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key
        self.message = f"Unknown store '{key}'. Choose {store_names()}."


def provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise UnknownStore(key) from None


def search_link(key: str, term: str) -> str:
    return provider(key).search_url.format(term=quote(term, safe=""))


def item_key(name: str) -> str:
    """Plain items have no UPC; the slug of the name is their identity (so "Bananas" twice is one row)."""
    slug = slugify(name)
    return "item" if slug == "list" else slug
```

Note `slugify("???")` returns `"list"`; `item_key` maps that to `"item"`. If storage.slugify's fallback is awkward here, give `slugify` a `fallback="list"` parameter instead.

**Step 4:** tests pass. **Step 5:** `git commit -m "feat(stores): provider table for Kroger and hand-off stores"`.

---

### Task 2: Storage keeps a store per list

**Files:** Modify `kroger_reorder/storage.py`; `tests/test_storage.py`.

**Step 1: Failing tests**

```python
def test_lists_default_to_kroger_and_persist_a_store(tmp_path):
    s = Storage(tmp_path)
    assert s.load_list(s.create_list("Weekly")).store == "kroger"
    slug = s.create_list("Aldi run", store="aldi")
    raw = json.loads((tmp_path / "lists" / f"{slug}.json").read_text(encoding="utf-8"))
    assert raw == {"name": "Aldi run", "store": "aldi", "items": []}
    assert s.load_list(slug).store == "aldi"
    assert [(l["slug"], l["store"]) for l in s.list_lists()] == [(slug, "aldi"), ("weekly", "kroger")]


def test_save_without_store_keeps_it_and_duplicate_copies_it(tmp_path):
    s = Storage(tmp_path)
    slug = s.create_list("Aldi run", store="aldi")
    s.save_list(slug, "Aldi trip", [milk()])          # rename-style save: store untouched
    assert s.load_list(slug).store == "aldi"
    s.rename_list(slug, "Aldi again")
    assert s.load_list(slug).store == "aldi"
    copy = s.duplicate_list(slug, "Copy")
    assert s.load_list(copy).store == "aldi"
    s.save_list(slug, "Aldi trip", [], store="meijer")  # explicit store wins
    assert s.load_list(slug).store == "meijer"


def test_file_without_store_field_is_a_kroger_list(tmp_path):
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "old.json").write_text('{"name": "Old", "items": []}', encoding="utf-8")
    assert Storage(tmp_path).load_list("old").store == "kroger"
```

Also update `test_create_list_writes_empty_file_and_returns_slug` to expect `"store": "kroger"` in the file.

**Step 3: Implementation.** In `storage.py`: `DEFAULT_STORE = "kroger"` (storage must not import stores.py: stores imports slugify from storage). `ListLoad` gains `store: str = DEFAULT_STORE`. `load_list` reads `str(raw.get("store") or DEFAULT_STORE)`. `save_list(self, slug, name, items, store=None)`: `if store is None: store = loaded.store if loaded is not None else DEFAULT_STORE`; writes `{"name", "store", "items"}`. `create_list(name, store=DEFAULT_STORE)` passes it on. `duplicate_list` passes `loaded.store`. `list_lists` rows add `"store": loaded.store`. Corrupt-file `ListLoad`s keep the default store.

**Step 5:** `git commit -m "feat(storage): a store per list, Kroger by default"`.

---

### Task 3: Create lists with a store; state and payloads carry it

**Files:** Modify `kroger_reorder/main.py`; `tests/test_routes.py`.

**Step 1: Failing tests**

```python
def test_state_lists_the_stores_and_each_lists_store(client):
    s = client.get("/api/state").json()
    assert [x["key"] for x in s["stores"]] == ["kroger", "aldi", "meijer", "walmart"]
    assert s["stores"][1] == {"key": "aldi", "name": "Aldi", "has_catalog": False}
    assert s["lists"][0]["store"] == "kroger"


def test_create_list_with_a_store(client):
    r = client.post("/api/lists", json={"name": "Aldi run", "store": "aldi"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert client.get(f"/api/lists/{slug}").json()["store"] == "aldi"
    assert client.post("/api/lists", json={"name": "X", "store": "costco"}).status_code == 400
    assert client.post("/api/lists", json={"name": "X", "store": "costco"}).json()["error"] == \
        "Unknown store 'costco'. Choose Kroger, Aldi, Meijer, or Walmart."


def test_hand_edited_unknown_store_is_400_not_500(client, tmp_path):
    (tmp_path / "lists" / "odd.json").write_text('{"name": "Odd", "store": "costco", "items": []}', encoding="utf-8")
    r = client.post("/api/lists/odd/items", json={"name": "milk"})
    assert r.status_code == 400 and "Unknown store 'costco'" in r.json()["error"]
```

**Step 3: Implementation.** `ListName` gains `store: str = DEFAULT_STORE`. `create_list` calls `provider(body.store)` before `storage.create_list(name, body.store)`. Exception handler for `UnknownStore` → `error(400, exc.message)`. `list_payload` adds `"store": loaded.store`. `/api/state` adds `"stores": [{"key", "name", "has_catalog"} for p in PROVIDERS.values()]`. Helper `provider_of(loaded) -> Provider` = `provider(loaded.store)` used by Tasks 4 and 5 (so a hand-edited unknown store is a 400 from any item route).

**Step 5:** `git commit -m "feat(lists): create a list for Aldi, Meijer or Walmart"`.

---

### Task 4: Plain items on hand-off lists; push and prices refused

**Files:** Modify `kroger_reorder/main.py`, `kroger_reorder/importer.py`; `tests/test_routes.py`, `tests/test_importer.py`.

**Step 1: Failing tests**

```python
@pytest.fixture
def aldi(client):
    return client.post("/api/lists", json={"name": "Aldi run", "store": "aldi"}).json()["slug"]


def test_plain_items_are_keyed_by_name_and_need_no_upc(client, aldi):
    r = client.post(f"/api/lists/{aldi}/items", json={"name": "Bananas"})
    assert r.status_code == 200
    assert r.json()["items"] == [{"upc": "bananas", "name": "Bananas", "size": "", "quantity": 1, "included": True,
                                  "image": None, "price": None, "promo": None, "price_checked_at": None}]
    client.post(f"/api/lists/{aldi}/items", json={"name": "bananas!", "quantity": 3})   # same key: no duplicate
    assert len(client.get(f"/api/lists/{aldi}").json()["items"]) == 1
    assert client.post(f"/api/lists/{aldi}/items", json={"name": "  "}).json()["error"] == "Give the item a name."
    assert client.delete(f"/api/lists/{aldi}/items/bananas").json()["items"] == []


def test_kroger_list_still_requires_a_upc(client):
    r = client.post("/api/lists/standard/items", json={"name": "Bananas"})
    assert r.status_code == 400 and r.json()["error"] == "Search your store and pick a product."


def test_push_and_prices_are_refused_on_a_hand_off_list(client, aldi, kroger):
    kroger.signed_in = True
    client.post(f"/api/lists/{aldi}/items", json={"name": "Bananas"})
    for path in ("push", "prices"):
        r = client.post(f"/api/lists/{aldi}/{path}", json=[] if path == "push" else None)
        assert r.status_code == 400
        assert r.json()["error"] == "This list is for Aldi; use Open at Aldi instead."
    assert kroger.cart_calls == []


def test_import_into_a_hand_off_list_keys_by_name_and_drops_kroger_fields(client, aldi):
    picks = [{"upc": "", "name": "Bananas", "quantity": 2},
             {"upc": "0001111041700", "name": "Kroger Milk", "size": "1 gal", "image": "https://img/1.jpg",
              "price": 3.49, "included": False, "quantity": 1},
             {"name": "bananas", "quantity": 1}]
    r = client.post(f"/api/lists/{aldi}/import", json=picks)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["upc"], i["name"], i["quantity"], i["included"], i["size"], i["image"], i["price"]) for i in items] == [
        ("bananas", "Bananas", 3, True, "", None, None), ("kroger-milk", "Kroger Milk", 1, False, "", None, None)]


def test_import_into_a_kroger_list_requires_upcs(client):
    r = client.post("/api/lists/standard/import", json=[{"name": "Bananas"}])
    assert r.status_code == 400 and r.json()["error"] == "Search your store and pick a product."
```

In `tests/test_importer.py`:

```python
def test_exact_rows_need_a_numeric_upc():
    # A hand-off list's export carries name keys in the upc column; those lines are matched by name again.
    lines = parse("upc,name,size,quantity,included,price,promo,image\nbananas,Bananas,,2,true,,,\n0001111041700,Milk,1 gal,1,true,3.49,,\n")
    assert [(l.name, l.quantity, l.exact is not None) for l in lines] == [("Bananas", 2, False), ("Milk", 1, True)]
```

**Step 3: Implementation.**

- `NewItem.upc: str = ""`, `NewItem.quantity: int = 1` (used only for plain items). `ImportItem.upc: str = ""`.
- `main.py` helpers:

```python
NO_NAME = "Give the item a name."
NO_UPC = "Search your store and pick a product."

def hand_off_refused(p: Provider) -> JSONResponse:
    return error(400, f"This list is for {p.name}; use Open at {p.name} instead.")

def plain_item(name: str, quantity: int = 1, included: bool = True) -> Item:
    return Item(upc=item_key(name), name=name.strip(), size="", quantity=max(1, quantity), included=included)
```

- `add_item`: `p = provider_of(loaded)`; if `not p.has_catalog`: `name = clean_name(new.name)` or 400 NO_NAME; item = `plain_item(name, new.quantity)`; append if key not present (no quantity bump, mirroring the Kroger branch). Else if `not new.upc`: 400 NO_UPC; existing code.
- `refresh_prices` and `push`: after `get_list`, `if not provider_of(loaded).has_catalog: return hand_off_refused(p)` (push: before saving the body, so nothing is written).
- `import_commit`: hand-off branch: for each pick, `name = clean_name(pick.name)` (skip blanks), `key = item_key(name)`; bump quantity if present else append `plain_item(name, pick.quantity, pick.included)`. Kroger branch: any pick without `upc` → 400 NO_UPC before writing.
- `importer._exact`: `if not upc.isdigit(): return None`.

**Step 5:** `git commit -m "feat(items): plain items on hand-off lists; cart and prices refused there"`.

---

### Task 5: The hand-off route

**Files:** Modify `kroger_reorder/main.py`; `tests/test_routes.py`.

**Step 1: Failing tests**

```python
def test_handoff_lists_checked_items_with_search_links_and_text(client, aldi):
    client.put(f"/api/lists/{aldi}/items", json=[
        {"upc": "bananas", "name": "Bananas", "quantity": 2},
        {"upc": "oat-milk", "name": "Oat milk & co", "quantity": 1},
        {"upc": "chips", "name": "Chips", "quantity": 1, "included": False}])
    r = client.get(f"/api/lists/{aldi}/handoff")
    assert r.status_code == 200
    assert r.json() == {
        "store": "aldi", "store_name": "Aldi", "home_url": "https://www.aldi.us/",
        "items": [{"name": "Bananas", "quantity": 2, "url": "https://www.aldi.us/store/aldi/s?k=Bananas"},
                  {"name": "Oat milk & co", "quantity": 1, "url": "https://www.aldi.us/store/aldi/s?k=Oat%20milk%20%26%20co"}],
        "text": "Aldi run\n- Bananas ×2\n- Oat milk & co"}


def test_handoff_on_a_kroger_list_is_400(client):
    r = client.get("/api/lists/standard/handoff")
    assert r.status_code == 400 and r.json()["error"] == "Kroger lists are sent to the cart instead."
```

**Step 3: Implementation.**

```python
@app.get("/api/lists/{slug}/handoff")
def handoff(slug: str):
    loaded = get_list(slug)
    p = provider_of(loaded)
    if p.has_catalog:
        return error(400, "Kroger lists are sent to the cart instead.")
    chosen = [i for i in loaded.items if i.included and i.quantity > 0]
    lines = [f"- {i.name}" + (f" ×{i.quantity}" if i.quantity > 1 else "") for i in chosen]
    return {"store": p.key, "store_name": p.name, "home_url": p.home_url,
            "items": [{"name": i.name, "quantity": i.quantity, "url": search_link(p.key, i.name)} for i in chosen],
            "text": "\n".join([loaded.name, *lines])}
```

**Step 5:** `git commit -m "feat(handoff): search links and a copyable list for hand-off stores"`.

---

### Task 6: The page

**Files:** Modify `kroger_reorder/static/index.html`, `kroger_reorder/static/app.js`. No automated tests (no JS harness); verify by hand on a scratch data dir and a spare port, with Playwright screenshots.

1. **State.** `state.stores = []` and `state.store_key = 'kroger'` (from the list payload's `store`). `refreshLists` copies `s.stores`; `adoptList` sets `state.store_key = data.store`. Helpers `providerOf(key)`, `currentProvider()`, `isHandOff()`.
2. **New-list dialog** `#new-list-dialog`: `<input id="new-list-name">`, `<select id="new-list-store">` filled from `state.stores`, Create / Cancel. `askNewList()` returns a Promise of `{name, store}` or null; used by the `new` action and by `pickTargetList` (replaces both `prompt` calls). Enter submits.
3. **List picker.** Option text: `${l.name}${l.store !== 'kroger' ? ' · ' + providerOf(l.store).name : ''} (${l.count})`.
4. **render() on a hand-off list.** `#add` visible regardless of the Kroger store; `#term` placeholder "Add an item, e.g. bananas"; `#search` text "Add"; `#search-results` empty. Hide `#store-line` (wrap the store label row in an id), `#price-row` (same), show `#handoff-line` ("Shopping at Aldi. Items are plain names; use Open at Aldi to shop them."). Item rows: no thumb, no price. Footer: `#push` hidden, `#open-store` shown with text `Open at Aldi`, enabled when any item is checked; footer note "Checked items are the ones you'll shop for." `#empty` text adjusted ("Type an item above…"). On a Kroger list everything is as today.
5. **Add on a hand-off list.** `search()` branches: `POST listPath('/items') {name}` then `adoptList`, clear, render.
6. **Hand-off panel** `#handoff-dialog`: heading `Shop this list at Aldi`, muted text explaining the search links, `<ul id="handoff-items">` rows `name ×qty` + `<a target="_blank" rel="noopener">Search at Aldi ↗</a>`, buttons **Copy list** (`navigator.clipboard.writeText(text)`, falls back to a selectable `<textarea>` when the clipboard API is unavailable, e.g. plain http on a phone), link **Open aldi.us ↗**, Close. Opened by `#open-store` via `GET listPath('/handoff')`.
7. **Import review on a hand-off list.** `reviewLines`: when `isHandOff()`, do not call resolve; `candidates = [{ upc: '', name: l.name }]`. Table header third column reads "Add?" and the select's search option reads "Use a different name…"; `runRowSearch` on a hand-off list sets `row.name = term; row.candidates = [{upc:'', name: term}]; row.searched = true` with no request. Kind presets (Skip for staple / on_list / stocked) unchanged. `importPicks` sends `{upc: '', name, quantity}` for plain rows.
8. **Photo/Recipe dialogs**: nothing else changes; the picker already lists every list.

Commit: `feat(page): new-list dialog with a store, hand-off lists and the Open at store panel`.

---

### Task 7: Docs and version

**Files:** `README.md`, `AGENTS.md`, `kroger_reorder/__init__.py` (7.0.0), `.env.example` unchanged.

README: tagline ("…and keeps hand-off lists for Aldi, Meijer and Walmart"); Features bullet **Other stores**; Project structure `stores.py`; Using the app: new subsection **Lists for Aldi, Meijer and Walmart**, List controls note on the + dialog; Data and privacy: `store` field and the `upc` column on hand-off exports; HTTP API: `POST /api/lists` purpose "Create a list (optional `store`)", `GET /api/lists/{slug}/handoff`, `/api/state` adds `stores`; Limitations: the other stores' sites; Troubleshooting rows for the four new strings; Design documents row v7; Version history row v7. AGENTS.md: layout row for `stores.py`, "Things deliberately not done" (scrapers, Instacart, store change after creation). Run `pytest -q`; commit `docs: v7 lists for Aldi, Meijer and Walmart; version 7.0.0`.

---

### Task 8: Finish

Full suite green, final code review, merge `multi-store` into `main`, push, watch CI, restart the live server on port 8000, update memory.
