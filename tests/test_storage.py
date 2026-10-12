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
