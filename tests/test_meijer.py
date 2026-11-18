"""MeijerClient against trimmed real Constructor.io responses (tests/fixtures/meijer_*), served by
httpx.MockTransport. The fixtures were edited to cover a sale, a by-weight item, an unbuyable and an
out-of-stock result, and a result with no variations."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from kroger_reorder.meijer import (CHANGED, LOOKUP_BATCH, MAX_PAGE, SEARCH_KEY, SLACK, VARIATIONS, MeijerClient,
                                   MeijerError, is_product, split_name)
from kroger_reorder.stores import CatalogError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEARCH = json.loads((FIXTURES / "meijer_search.json").read_text(encoding="utf-8"))
ITEMS = json.loads((FIXTURES / "meijer_items.json").read_text(encoding="utf-8"))


class FakeService:
    """Routes requests the way ac.cnstrc.com does; records them; lets a test override any answer."""

    def __init__(self):
        self.calls = []          # (request, raw path, query dict of lists)
        self.search = copy.deepcopy(SEARCH)
        self.items = copy.deepcopy(ITEMS)
        self.overrides = []      # httpx.Response or Exception, consumed in order before any routing

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.calls.append((request, request.url.raw_path.decode().split("?")[0], parse_qs(url.query)))
        if self.overrides:
            answer = self.overrides.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if url.path.startswith("/search/"):
            per_page = int(parse_qs(url.query).get("num_results_per_page", ["20"])[0])
            return httpx.Response(200, json={"response": {"results": self.search["response"]["results"][:per_page]}})
        if url.path == "/browse/items":
            wanted = parse_qs(url.query).get("ids", [])
            by_id = {r["data"]["id"]: r for r in self.items["response"]["results"]}
            return httpx.Response(200, json={"response": {"results": [by_id[i] for i in wanted if i in by_id]}})
        return httpx.Response(404, json={})

    def paths(self):
        return [path for _, path, _ in self.calls]

    def last_query(self):
        return self.calls[-1][2]


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def client(service):
    return MeijerClient(transport=httpx.MockTransport(service.handler))


def test_search_returns_products_in_order(client, service):
    results = client.search_products("milk", limit=3)
    # The unbuyable half gallon of whole milk is dropped; the rest keep the service's order.
    assert [r["upc"] for r in results] == ["meijer:4125010200", "meijer:4125010210", "meijer:4125010211"]
    assert results[1] == {
        "upc": "meijer:4125010210", "name": "Meijer 2% Reduced Fat Milk", "brand": "", "size": "Gallon",
        "price": 3.09, "promo": None, "in_stock": True,
        "image": "https://www.meijer.com/content/dam/meijer/product/0041/25/0102/10/0041250102106_2_A1C1_0200.jpg",
    }
    request, path, query = service.calls[-1]
    assert str(request.url).startswith("https://ac.cnstrc.com/search/milk?")
    assert path == "/search/milk"
    assert query["key"] == [SEARCH_KEY]
    assert query["num_results_per_page"] == [str(3 + SLACK)]   # room for dropped results
    assert json.loads(query["variations_map"][0]) == json.loads(VARIATIONS)


def test_search_term_is_one_encoded_path_segment(client, service):
    client.search_products("2% milk")
    client.search_products("milk/eggs")
    client.search_products("mac & cheese?")
    assert service.paths() == ["/search/2%25%20milk", "/search/milk%2Feggs", "/search/mac%20%26%20cheese%3F"]


def test_search_honours_limit(client, service):
    assert [r["upc"] for r in client.search_products("milk", limit=1)] == ["meijer:4125010200"]
    assert len(client.search_products("milk", limit=4)) == 4
    client.search_products("milk", limit=500)
    client.search_products("milk", limit=0)
    assert [q["num_results_per_page"] for _, _, q in service.calls[-2:]] == [[str(MAX_PAGE)], [str(1 + SLACK)]]


def test_search_asks_for_slack_so_a_dropped_hit_does_not_shorten_the_answer(client, service):
    # The third hit is unbuyable: asking for exactly three would return two.
    results = client.search_products("milk", limit=3)
    assert [r["upc"] for r in results] == ["meijer:4125010200", "meijer:4125010210", "meijer:4125010211"]


@pytest.mark.parametrize("term", ["", " ", ".", "..", "#?&", " - "])
def test_a_term_without_letters_or_digits_sends_nothing(client, service, term):
    assert client.search_products(term) == []
    assert service.calls == []


def test_search_term_is_normalised(client, service):
    client.search_products("  2%   milk\t\n")
    client.search_products("a" * 300)
    assert service.paths() == ["/search/2%25%20milk", "/search/" + "a" * MAX_PAGE]


def test_price_is_the_median_store_price(client):
    by_upc = {r["upc"]: r for r in client.search_products("milk")}
    assert by_upc["meijer:4125010200"]["price"] == 3.02    # seven stores: the middle one
    assert by_upc["meijer:4125010210"]["price"] == 3.09    # six stores: halfway between 3.08 and 3.10


def test_promo_is_the_median_sale_price_when_lower(client):
    found = client.lookup_products(["meijer:4125010210", "meijer:4011"])
    milk = found["meijer:4125010210"]
    assert (milk["price"], milk["promo"]) == (3.09, 2.54)  # on sale at four of six stores
    # Not on sale: the service repeats the price as the "sale" price, which is not a promo.
    assert (found["meijer:4011"]["price"], found["meijer:4011"]["promo"]) == (0.28, None)


def test_price_uses_the_variations_in_the_listed_unit(client, service):
    ham = service.items["response"]["results"][2]          # priceUnit LB
    ham["variations_map"].insert(0, {"unit": "EA", "prices": [1.0] * 5, "sale_prices": [0.5] * 5})
    found = client.lookup_products(["meijer:21180600000"])["meijer:21180600000"]
    assert (found["price"], found["promo"]) == (10.99, None)
    ham["data"]["priceUnit"] = "KG"                        # no group in that unit: every group counts
    found = client.lookup_products(["meijer:21180600000"])["meijer:21180600000"]
    assert (found["price"], found["promo"]) == (1.0, 0.5)


def test_without_variations_the_listed_price_stands_in(client, service):
    eggs = client.lookup_products(["meijer:71373381494"])["meijer:71373381494"]
    assert (eggs["price"], eggs["promo"]) == (1.69, None)
    result = service.items["response"]["results"][3]
    del result["variations_map"]
    result["data"]["price"] = None
    eggs = client.lookup_products(["meijer:71373381494"])["meijer:71373381494"]
    assert (eggs["price"], eggs["promo"]) == (None, None)


@pytest.mark.parametrize("title, by_weight, unit, expected", [
    ("Meijer 2% Reduced Fat Milk, Gallon", False, "EA", ("Meijer 2% Reduced Fat Milk", "Gallon")),
    ("Organic Bananas, 5-8 ct", False, "LB", ("Organic Bananas", "5-8 ct")),
    ("True Goodness, 85/15 Organic Grass Fed Ground Beef, 3 lb", False, "EA",
     ("True Goodness, 85/15 Organic Grass Fed Ground Beef", "3 lb")),
    ("Snacks, Assorted Flavors Variety Pack of Thirty", False, "EA",
     ("Snacks, Assorted Flavors Variety Pack of Thirty", "")),      # the tail is too long to be a size
    ("Bananas", False, "LB", ("Bananas", "")),                     # LB without priceByWeight is sold each
    ("Frederik's by Meijer Black Forest Ham", True, "LB", ("Frederik's by Meijer Black Forest Ham", "per lb")),
    ("Meijer Deli Turkey, 1 lb", True, "LB", ("Meijer Deli Turkey", "1 lb")),
    (", Gallon", False, "EA", (", Gallon", "")),
])
def test_name_and_size(title, by_weight, unit, expected):
    assert split_name(title, by_weight, unit) == expected


def test_by_weight_items_are_sold_per_lb(client):
    found = client.lookup_products(["meijer:21180600000", "meijer:4011"])
    assert (found["meijer:21180600000"]["size"], found["meijer:21180600000"]["price"]) == ("per lb", 10.99)
    assert found["meijer:4011"]["size"] == ""


def test_unbuyable_results_are_dropped(client, service):
    results = service.search["response"]["results"]
    results[0]["data"]["deactivated"] = True
    results[1]["data"]["id"] = "not-a-number"
    results.append("junk")
    assert [r["upc"] for r in client.search_products("milk")] == ["meijer:4125010211", "meijer:4125010268"]


def test_out_of_stock_is_flagged(client):
    by_upc = {r["upc"]: r for r in client.search_products("milk")}
    assert by_upc["meijer:4125010211"]["in_stock"] is False
    assert by_upc["meijer:4125010200"]["in_stock"] is True


def test_lookup_products_batches_ids(client, service):
    found = client.lookup_products(["meijer:4125010210", "meijer:4011", "4011", "milk", "meijer:4011"])
    assert set(found) == {"meijer:4125010210", "meijer:4011"}
    assert found["meijer:4011"]["name"] == "Bananas"
    assert service.paths() == ["/browse/items"]
    assert service.last_query()["ids"] == ["4125010210", "4011"]
    assert service.last_query()["num_results_per_page"] == ["2"]
    assert "variations_map" in service.last_query()


def test_lookup_without_meijer_ids_sends_nothing(client, service):
    assert client.lookup_products(["4011", "milk", "aldi:123", "meijer:abc"]) == {}
    assert service.calls == []


def test_lookup_of_many_products_is_split_into_requests(client, service):
    upcs = [f"meijer:{n}" for n in range(1, LOOKUP_BATCH + 3)] + ["meijer:4011"]
    found = client.lookup_products(upcs)
    assert list(found) == ["meijer:4011"]      # unknown ids are simply absent
    sizes = [len(q["ids"]) for _, _, q in service.calls]
    assert sizes == [LOOKUP_BATCH, 3]


HTTP_400 = "Meijer's search said HTTP 400."


@pytest.mark.parametrize("answer, status, message, unavailable", [
    (httpx.ConnectError("boom"), 0, "Couldn't reach Meijer's search: ConnectError", True),
    (httpx.ReadTimeout("slow"), 0, "Couldn't reach Meijer's search: ReadTimeout", True),
    (httpx.Response(500, text="oops"), 502, "Meijer's search said HTTP 500.", False),
    (httpx.Response(429, json={}), 502, "Meijer's search said HTTP 429.", False),
    # What Constructor really answers for an unknown key:
    (httpx.Response(400, json={"message": "You have supplied an invalid `key` or `autocomplete_key`. You can "
                                          "find your key at app.constructor.io/dashboard/accounts/api_integration."}),
     502, CHANGED, True),
    (httpx.Response(400, json={"message": "Invalid KEY"}), 502, CHANGED, True),
    (httpx.Response(400, json={"message": "num_results_per_page must be at most 200"}), 502, HTTP_400, False),
    (httpx.Response(400, json=["key"]), 502, HTTP_400, False),
    (httpx.Response(400, text="Bad Request"), 502, HTTP_400, False),
    (httpx.Response(400), 502, HTTP_400, False),
    (httpx.Response(401, json={}), 502, CHANGED, True),
    (httpx.Response(403, json={}), 502, CHANGED, True),
    (httpx.Response(200, json={"response": {}}), 502, CHANGED, True),
    (httpx.Response(200, json={"response": {"results": {}}}), 502, CHANGED, True),
    (httpx.Response(200, json=[]), 502, CHANGED, True),
    (httpx.Response(200, text="<html>Access Denied</html>"), 502, CHANGED, True),
])
def test_errors(client, service, answer, status, message, unavailable):
    service.overrides.append(answer)
    with pytest.raises(MeijerError) as err:
        client.search_products("milk")
    assert (err.value.status, err.value.message) == (status, message)
    assert isinstance(err.value, CatalogError)
    assert err.value.unavailable is unavailable


def test_lookup_errors_raise_too(client, service):
    service.overrides.append(httpx.Response(503))
    with pytest.raises(MeijerError, match="HTTP 503"):
        client.lookup_products(["meijer:4011"])


def test_only_constructor_is_called(client, service):
    service.overrides.append(httpx.Response(200, json=SEARCH, headers={"set-cookie": "ConstructorioID=abc; Path=/"}))
    client.search_products("milk")
    client.search_products("eggs")
    client.lookup_products(["meijer:4011"])
    assert len(service.calls) == 3
    for request, _, _ in service.calls:
        assert request.url.scheme == "https" and request.url.host == "ac.cnstrc.com"
        assert "cookie" not in request.headers


def test_key_override(service):
    client = MeijerClient(transport=httpx.MockTransport(service.handler), search_key="key_other")
    client.search_products("milk")
    assert service.last_query()["key"] == ["key_other"]


def test_is_product():
    assert is_product("meijer:4011")
    assert not is_product("4011")
    assert not is_product("meijer:")
    assert not is_product("aldi:4011")
    assert not is_product("meijer:abc")
