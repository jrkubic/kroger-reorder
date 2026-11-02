# tests/test_stores.py
import pytest

from kroger_reorder.stores import (DEFAULT_STORE, PROVIDERS, UnknownStore, item_key, provider, search_link,
                                   store_names)


def test_kroger_is_the_default_and_the_only_catalog_store():
    assert DEFAULT_STORE == "kroger"
    assert [k for k, p in PROVIDERS.items() if p.has_catalog] == ["kroger"]
    assert list(PROVIDERS) == ["kroger", "aldi", "meijer", "walmart"]


def test_hand_off_stores_have_https_search_and_home_links():
    for key in ("aldi", "meijer", "walmart"):
        p = provider(key)
        assert p.search_url.startswith("https://") and "{term}" in p.search_url
        assert p.home_url.startswith("https://")
    assert provider("kroger").search_url is None and provider("kroger").home_url is None


def test_search_link_encodes_the_term():
    assert search_link("walmart", "2% milk & eggs") == "https://www.walmart.com/search?q=2%25%20milk%20%26%20eggs"
    assert search_link("aldi", "eggs") == "https://www.aldi.us/store/aldi/s?k=eggs"
    assert search_link("meijer", "eggs") == "https://www.meijer.com/shopping/search.html?text=eggs"


def test_search_link_refuses_a_catalog_store():
    with pytest.raises(ValueError):
        search_link("kroger", "eggs")


def test_unknown_store_raises_with_a_user_message():
    with pytest.raises(UnknownStore) as exc:
        provider("costco")
    assert exc.value.key == "costco"
    assert exc.value.message == "Unknown store 'costco'. Choose Kroger, Aldi, Meijer, or Walmart."


def test_item_key_is_a_name_slug():
    assert item_key("  Bananas! ") == "bananas"
    assert item_key("Oat milk & co") == "oat-milk-co"
    assert item_key("???") == "item"


def test_store_names_for_messages():
    assert store_names() == "Kroger, Aldi, Meijer, or Walmart"
