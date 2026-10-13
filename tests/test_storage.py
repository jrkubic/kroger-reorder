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
