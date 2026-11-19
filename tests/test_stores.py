# tests/test_stores.py
import pytest

from kroger_reorder.aldi import CHANGED, AldiError
from kroger_reorder.stores import (DEFAULT_STORE, PROVIDERS, CatalogError, UnknownStore, item_key, provider,
                                   search_link, is_product, shop_link, store_names)


def test_kroger_is_the_default_and_the_only_cart_store():
    assert DEFAULT_STORE == "kroger"
    assert [k for k, p in PROVIDERS.items() if p.cart] == ["kroger"]
    assert [k for k, p in PROVIDERS.items() if p.catalog] == ["kroger", "aldi", "meijer"]
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


def test_is_product_is_scoped_to_the_store():
    assert is_product("kroger", "0001111041700") and not is_product("kroger", "aldi:16902710")
    assert is_product("aldi", "aldi:16902710")
    assert not is_product("aldi", "16902710")        # a Kroger UPC, or a plain item named "16902710"
    assert not is_product("aldi", "whole-milk") and not is_product("aldi", "aldi:")
    assert not is_product("meijer", "123")


def test_meijer_products_carry_the_meijer_prefix():
    assert is_product("meijer", "meijer:4125010210")
    for upc in ("aldi:4125010210", "4125010210", "whole-milk", "meijer:"):
        assert not is_product("meijer", upc), upc
    assert not is_product("aldi", "meijer:4125010210") and not is_product("kroger", "meijer:4125010210")


def test_meijer_products_open_their_product_page():
    # meijer.com finds a product page by the id alone (checked in Jake's browser with "x" in place of
    # the name), so the name part is only there to read well.
    assert shop_link("meijer", "meijer:4125010210", "Meijer 2% Reduced Fat Milk") == (
        "https://www.meijer.com/shopping/product/meijer-2-reduced-fat-milk/4125010210.html")
    assert shop_link("meijer", "meijer:4011", "") == "https://www.meijer.com/shopping/product/item/4011.html"
    assert shop_link("meijer", "candles", "Candles") == "https://www.meijer.com/shopping/search.html?text=Candles"


def test_only_meijer_prices_are_approximate():
    assert [k for k, p in PROVIDERS.items() if p.approx_prices] == ["meijer"]


def test_shop_link_uses_the_product_page_for_catalog_products():
    assert shop_link("aldi", "aldi:16902710", "Whole Milk") == "https://www.aldi.us/store/aldi/products/16902710"
    assert shop_link("aldi", "16902710", "Whole Milk") == "https://www.aldi.us/store/aldi/s?k=Whole%20Milk"
    assert shop_link("aldi", "whole-milk", "Whole Milk") == "https://www.aldi.us/store/aldi/s?k=Whole%20Milk"
    assert shop_link("meijer", "123", "Eggs") == "https://www.meijer.com/shopping/search.html?text=Eggs"


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


def test_store_errors_share_a_base():
    exc = AldiError(502, CHANGED)
    assert isinstance(exc, CatalogError) and exc.unavailable
    assert CatalogError(0, "Couldn't reach x").unavailable and not CatalogError(502, "HTTP 500").unavailable
