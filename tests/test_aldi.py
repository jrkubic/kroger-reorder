# tests/test_aldi.py
"""AldiClient against trimmed real aldi.us responses (tests/fixtures/aldi_*), served by httpx.MockTransport."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from kroger_reorder.aldi import (CHANGED, ITEMS_HASH, SEARCH_HASH, SESSION_MAX_AGE, AldiClient, AldiError,
                                 Context, is_product, parse_price)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE = (FIXTURES / "aldi_page.html").read_text(encoding="utf-8")
SEARCH = json.loads((FIXTURES / "aldi_search.json").read_text(encoding="utf-8"))
ITEMS = json.loads((FIXTURES / "aldi_items.json").read_text(encoding="utf-8"))
LOCATION = json.loads((FIXTURES / "aldi_location.json").read_text(encoding="utf-8"))
NOT_SUPPORTED = {"errors": [{"message": "PersistedQueryNotSupported",
                             "extensions": {"code": "PERSISTED_QUERY_NOT_SUPPORTED"}}], "data": None}


class FakeSite:
    """Routes requests the way aldi.us does; records them; lets a test override any answer."""

    def __init__(self):
        self.calls = []          # (operationName or "page", query dict)
        self.items = copy.deepcopy(ITEMS)
        self.search = copy.deepcopy(SEARCH)
        self.overrides = {}      # operationName or "page" -> list of httpx.Response or Exception, consumed in order

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        op = "page" if url.path == "/store/aldi/s" else query.get("operationName", "?")
        self.calls.append((op, query))
        queued = self.overrides.get(op)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if op == "page":
            return httpx.Response(200, text=PAGE, headers={"set-cookie": "session=abc; Path=/"})
        if op == "SearchResultsPlacements":
            return httpx.Response(200, json=self.search)
        if op == "Items":
            wanted = json.loads(query["variables"])["ids"]
            by_id = {i["id"]: i for i in self.items["data"]["items"]}
            return httpx.Response(200, json={"data": {"items": [by_id[i] for i in wanted if i in by_id]}})
        if op == "GetRetailerLocationAddress":
            return httpx.Response(200, json=LOCATION)
        return httpx.Response(404, json={})

    def ops(self):
        return [op for op, _ in self.calls]

    def last(self, op):
        return [q for o, q in self.calls if o == op][-1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(site, clock):
    return AldiClient(transport=httpx.MockTransport(site.handler), clock=clock)


def test_session_parses_context_from_page_once(client, site):
    client.search_products("bananas")
    client.search_products("strawberries")
    assert client.context == Context(postal_code="43040", shop_id="48408", zone_id="787", location_id="24032")
    assert site.ops().count("page") == 1


def test_session_is_renewed_after_its_max_age(client, site, clock):
    client.search_products("milk")
    clock.now += SESSION_MAX_AGE + 1
    client.search_products("milk")
    assert site.ops().count("page") == 2


def test_search_returns_ranked_grid_products_with_prices(client, site):
    results = client.search_products("bananas")
    # Grid placements in order; the fixture's Items answer covers three of them, the rest are skipped.
    assert [r["upc"] for r in results] == ["aldi:25720157", "aldi:16383764", "aldi:3111634"]
    first = results[0]
    assert first == {
        "upc": "aldi:25720157", "name": "Bananas, per lb", "brand": "", "size": "per lb",
        "price": 0.16, "promo": None, "in_stock": True,
        "image": first["image"], "url": "https://www.aldi.us/store/aldi/products/25720157",
    }
    assert first["image"].startswith("https://www.instacart.com/image-server/296x296/")
    search_vars = json.loads(site.last("SearchResultsPlacements")["variables"])
    assert search_vars["query"] == "bananas"
    assert (search_vars["shopId"], search_vars["postalCode"], search_vars["zoneId"]) == ("48408", "43040", "787")
    items_vars = json.loads(site.last("Items")["variables"])
    assert items_vars["ids"][:3] == ["items_24032-25720157", "items_24032-3255474", "items_24032-23974682"]
    assert len(items_vars["ids"]) == 10   # limit
    assert json.loads(site.last("Items")["extensions"])["persistedQuery"]["sha256Hash"] == ITEMS_HASH


def test_search_ignores_carousels_and_honours_limit(client, site):
    results = client.search_products("bananas", limit=1)
    assert [r["upc"] for r in results] == ["aldi:25720157"]
    assert json.loads(site.last("Items")["variables"])["ids"] == ["items_24032-25720157"]


def test_search_with_no_grid_results_is_empty_and_skips_items(client, site):
    site.search["data"]["searchResultsPlacements"]["placements"] = []
    assert client.search_products("zzz") == []
    assert "Items" not in site.ops()


def test_sale_price_maps_to_promo(client, site):
    section = site.items["data"]["items"][1]["price"]["viewSection"]
    section.update(fullPriceString="$3.99", priceString="$2.99")
    straw = next(r for r in client.search_products("x") if r["upc"] == "aldi:16383764")
    assert (straw["price"], straw["promo"]) == (3.99, 2.99)


@pytest.mark.parametrize("text, expected", [
    ("$2.57", 2.57), ("$ 3", 3.0), ("$1,299.00", 1299.0), (" $0.16 ", 0.16),
    ("2 for $5.00", None), ("$1.39/lb", None), ("$0.16 each (est.)", None), ("", None), (None, None),
])
def test_parse_price_only_takes_plain_dollar_amounts(text, expected):
    assert parse_price(text) == expected


def test_unparseable_price_string_falls_back_to_the_value_string_or_none(client, site):
    first, second = site.items["data"]["items"][:2]
    first["price"]["viewSection"].update(priceString="2 for $5.00", priceValueString="2.50")
    second["price"]["viewSection"].update(priceString="$1.39/lb", priceValueString=None)
    by = {r["upc"]: r for r in client.search_products("x")}
    assert by["aldi:25720157"]["price"] == 2.5
    assert by["aldi:16383764"]["price"] is None


def test_an_entry_without_a_product_id_is_skipped_not_fatal(client, site):
    del site.items["data"]["items"][0]["productId"]
    assert [r["upc"] for r in client.search_products("x")] == ["aldi:16383764", "aldi:3111634"]


def test_out_of_stock_and_brand(client, site):
    item = site.items["data"]["items"][2]
    item["availability"] = {"available": False, "stockLevel": "outOfStock"}
    item["brandName"] = "Simply Nature"
    avocado = next(r for r in client.search_products("x") if r["upc"] == "aldi:3111634")
    assert avocado["in_stock"] is False and avocado["brand"] == "Simply Nature"


def test_lookup_products_batches_ids_and_skips_missing_and_foreign(client, site):
    found = client.lookup_products(["aldi:25720157", "aldi:3111634", "aldi:999", "0001111041700", "bananas"])
    assert sorted(found) == ["aldi:25720157", "aldi:3111634"]
    assert found["aldi:3111634"]["name"] == "Large Avocados, each"
    assert site.ops().count("Items") == 1
    assert json.loads(site.last("Items")["variables"])["ids"] == [
        "items_24032-25720157", "items_24032-3111634", "items_24032-999"]
    assert client.lookup_products([]) == {} and client.lookup_products(["123"]) == {}


def test_is_product():
    assert is_product("aldi:123") and not is_product("123") and not is_product("aldi-milk") and not is_product("aldi:")


def test_store_names_the_location(client):
    assert client.store() == {"store_id": "24032", "store_name": "ALDI - SPR 20 - Marysville",
                              "address": "15740 US-36 E, Marysville, OH 43040"}


def test_transport_failure_is_not_retried(client, site):
    site.overrides["page"] = [httpx.ConnectError("boom")]
    with pytest.raises(AldiError) as exc:
        client.search_products("milk")
    assert exc.value.status == 0 and exc.value.message == "Couldn't reach aldi.us: ConnectError"
    assert exc.value.unavailable


def test_http_error_after_one_fresh_session(client, site):
    site.overrides["SearchResultsPlacements"] = [httpx.Response(500, text="oops"), httpx.Response(500, text="oops")]
    with pytest.raises(AldiError) as exc:
        client.search_products("milk")
    assert (exc.value.status, exc.value.message) == (502, "Aldi's site said HTTP 500.")
    assert not exc.value.unavailable
    assert site.ops().count("page") == 2


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(200, json={"errors": [{"message": "PersistedQueryNotFound"}], "data": None}),
    lambda: httpx.Response(200, json={"data": {"somethingElse": {}}}),
    lambda: httpx.Response(200, text="not json"),
    lambda: httpx.Response(400, json=NOT_SUPPORTED),   # what aldi.us answered on 2026-11-05 for an unknown hash
])
def test_changed_site(client, site, answer):
    site.overrides["SearchResultsPlacements"] = [answer(), answer()]
    with pytest.raises(AldiError) as exc:
        client.search_products("milk")
    assert (exc.value.status, exc.value.message) == (502, CHANGED) and exc.value.unavailable


def test_any_failure_gets_one_fresh_session(client, site):
    client.search_products("milk")
    site.overrides["SearchResultsPlacements"] = [httpx.Response(200, json={"errors": [{"message": "expired"}]})]
    assert client.search_products("milk")                 # retried on a new session
    assert site.ops().count("page") == 2


def test_forbidden_twice_is_an_error(client, site):
    site.overrides["SearchResultsPlacements"] = [httpx.Response(403), httpx.Response(403)]
    with pytest.raises(AldiError) as exc:
        client.search_products("milk")
    assert exc.value.message == "Aldi's site said HTTP 403."


def test_threads_that_saw_the_same_failure_share_one_refresh(client, site):
    first = client._session()
    second = client._session(stale=first)      # first thread to notice: refreshes
    third = client._session(stale=first)       # a later thread with the same stale session: reuses it
    assert second is third and second is not first and first.retired
    assert site.ops().count("page") == 2


def test_page_without_context_is_a_changed_site(client, site):
    site.overrides["page"] = [httpx.Response(200, text="<html>nothing here</html>")]
    with pytest.raises(AldiError) as exc:
        client.search_products("milk")
    assert exc.value.message == CHANGED


def test_hash_overrides(site):
    client = AldiClient(transport=httpx.MockTransport(site.handler), search_hash="a" * 64, items_hash="b" * 64)
    client.search_products("milk")
    assert json.loads(site.last("SearchResultsPlacements")["extensions"])["persistedQuery"]["sha256Hash"] == "a" * 64
    assert json.loads(site.last("Items")["extensions"])["persistedQuery"]["sha256Hash"] == "b" * 64
    assert SEARCH_HASH != "a" * 64
