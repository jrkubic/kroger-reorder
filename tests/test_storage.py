# tests/test_storage.py
from app.storage import Item, Storage


def test_list_roundtrip(tmp_path):
    s = Storage(tmp_path)
    items = [Item(upc="0001", name="Milk", size="1 gal", quantity=2, included=False)]
    s.save_list(items)
    loaded = s.load_list()
    assert loaded.items == items
    assert loaded.warning is None


def test_missing_list_is_empty_without_warning(tmp_path):
    loaded = Storage(tmp_path).load_list()
    assert loaded.items == []
    assert loaded.warning is None


def test_corrupt_list_is_empty_with_warning_and_file_untouched(tmp_path):
    (tmp_path / "list.json").write_text("{not json", encoding="utf-8")
    loaded = Storage(tmp_path).load_list()
    assert loaded.items == []
    assert "not valid JSON" in loaded.warning
    assert (tmp_path / "list.json").read_text(encoding="utf-8") == "{not json"


def test_non_array_list_is_empty_with_warning(tmp_path):
    (tmp_path / "list.json").write_text('{"upc": "1"}', encoding="utf-8")
    loaded = Storage(tmp_path).load_list()
    assert loaded.items == []
    assert "array" in loaded.warning


def test_config_roundtrip_and_default(tmp_path):
    s = Storage(tmp_path)
    assert s.load_config() == {}
    s.save_config({"store_id": "01400943", "store_name": "Kroger Hyde Park"})
    assert s.load_config()["store_id"] == "01400943"


def test_tokens_roundtrip_and_clear(tmp_path):
    s = Storage(tmp_path)
    assert s.load_tokens() is None
    s.save_tokens({"access_token": "a", "refresh_token": "r", "expires_at": 123.0})
    assert s.load_tokens()["refresh_token"] == "r"
    s.clear_tokens()
    assert s.load_tokens() is None
    s.clear_tokens()  # clearing twice must not raise


def test_list_coerces_quantity_string(tmp_path):
    content = '[{"upc": "1", "name": "x", "size": "", "quantity": "2"}]'
    (tmp_path / "list.json").write_text(content, encoding="utf-8")
    loaded = Storage(tmp_path).load_list()
    assert loaded.items == [Item(upc="1", name="x", size="", quantity=2, included=True)]
    assert loaded.warning is None


def test_list_entry_with_bad_fields_is_empty_with_warning(tmp_path):
    content = '[{"upc": "1", "name": "x", "size": "", "quantity": "two"}]'
    (tmp_path / "list.json").write_text(content, encoding="utf-8")
    loaded = Storage(tmp_path).load_list()
    assert loaded.items == []
    assert "unexpected or missing fields" in loaded.warning
    assert (tmp_path / "list.json").read_text(encoding="utf-8") == content


def test_non_object_config_is_empty(tmp_path):
    (tmp_path / "config.json").write_text("[1]", encoding="utf-8")
    assert Storage(tmp_path).load_config() == {}


def test_concurrent_writes_do_not_crash(tmp_path):
    import threading

    s = Storage(tmp_path)
    written = [{"n": i} for i in range(8)]
    failures: list[BaseException] = []

    def save(cfg: dict) -> None:
        try:
            s.save_config(cfg)
        except BaseException as exc:  # noqa: BLE001 - surface anything a worker hit
            failures.append(exc)

    threads = [threading.Thread(target=save, args=(cfg,)) for cfg in written]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    assert s.load_config() in written
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_list_backs_up_corrupt_file(tmp_path):
    (tmp_path / "list.json").write_text("{not json", encoding="utf-8")
    s = Storage(tmp_path)
    s.save_list([Item(upc="0001", name="Milk", size="1 gal")])
    backups = list(tmp_path.glob("list.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json"
    assert s.load_list().items == [Item(upc="0001", name="Milk", size="1 gal")]
