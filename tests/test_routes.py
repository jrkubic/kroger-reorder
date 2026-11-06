# tests/test_routes.py
import base64
import json
import re

import pytest
from fastapi.testclient import TestClient

from kroger_reorder.aldi import CHANGED, SIGNED_OUT, AldiError, CartResult
from kroger_reorder.kroger import KrogerError, NotSignedIn
from kroger_reorder.main import create_app
from kroger_reorder.recipe import Extracted, Page, RecipeError
from kroger_reorder.storage import Item, Storage
from kroger_reorder.vision import Suggestion, VisionError


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
        "lists": [{"slug": "standard", "name": "Standard", "count": 0, "store": "kroger"}],
        "active_list": "standard",
        "photo_backend": None, "public_url": "http://localhost:8000",
        # No Aldi client in this app: Aldi lists are plain until one is configured.
        "stores": [{"key": "kroger", "name": "Kroger", "catalog": True, "cart": True, "linked_cart": False},
                   {"key": "aldi", "name": "Aldi", "catalog": False, "cart": False, "linked_cart": False},
                   {"key": "meijer", "name": "Meijer", "catalog": False, "cart": False, "linked_cart": False},
                   {"key": "walmart", "name": "Walmart", "catalog": False, "cart": False, "linked_cart": False}],
        "aldi_account": None,
    }


def test_state_reports_photo_backend_and_public_url(storage, kroger):
    app = create_app(storage, kroger, photo_backend="api", public_url="http://nas.local:8000")
    s = TestClient(app).get("/api/state").json()
    assert s["photo_backend"] == "api" and s["public_url"] == "http://nas.local:8000"


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


def test_index_versions_the_script_and_disables_stale_caching(client):
    # A browser that cached an old app.js must not run it against a newer page: the script URL
    # changes whenever the file does, and both page and static files must always be revalidated.
    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    m = re.search(r'src="/static/app\.js\?v=(\d+)"', r.text)
    assert m, "index.html should reference a versioned app.js"
    js = client.get(f"/static/app.js?v={m.group(1)}")
    assert js.status_code == 200
    assert js.headers["cache-control"] == "no-cache"


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


# -- other stores (hand-off lists) ------------------------------------------------------

@pytest.fixture
def aldi(client):
    return client.post("/api/lists", json={"name": "Aldi run", "store": "aldi"}).json()["slug"]


def test_create_list_with_a_store(client, aldi):
    loaded = client.get(f"/api/lists/{aldi}").json()
    assert loaded["store"] == "aldi" and loaded["name"] == "Aldi run"
    assert client.get("/api/lists/standard").json()["store"] == "kroger"
    r = client.post("/api/lists", json={"name": "X", "store": "costco"})
    assert r.status_code == 400
    assert r.json()["error"] == "Unknown store 'costco'. Choose Kroger, Aldi, Meijer, or Walmart."


def test_duplicate_keeps_the_store(client, aldi):
    copy = client.post(f"/api/lists/{aldi}/duplicate", json={"name": "Aldi copy"}).json()["slug"]
    assert client.get(f"/api/lists/{copy}").json()["store"] == "aldi"


def test_hand_edited_unknown_store_is_400_not_500(client, tmp_path):
    (tmp_path / "lists" / "odd.json").write_text('{"name": "Odd", "store": "costco", "items": []}', encoding="utf-8")
    r = client.post("/api/lists/odd/items", json={"name": "milk"})
    assert r.status_code == 400 and r.json()["error"].startswith("Unknown store 'costco'")


def test_plain_items_are_keyed_by_name_and_need_no_upc(client, aldi):
    r = client.post(f"/api/lists/{aldi}/items", json={"name": " Bananas "})
    assert r.status_code == 200
    assert r.json()["items"] == [{"upc": "bananas", "name": "Bananas", "size": "", **FULL}]
    r = client.post(f"/api/lists/{aldi}/items", json={"name": "bananas!", "quantity": 3})   # same key: one row, more of it
    assert [(i["name"], i["quantity"]) for i in r.json()["items"]] == [("Bananas", 4)]
    r = client.post(f"/api/lists/{aldi}/items", json={"name": "Eggs", "quantity": 2})
    assert r.json()["items"][1]["quantity"] == 2
    assert client.post(f"/api/lists/{aldi}/items", json={"name": "  "}).json()["error"] == "Give the item a name."
    assert client.delete(f"/api/lists/{aldi}/items/bananas").json()["items"][0]["upc"] == "eggs"


def test_kroger_list_still_requires_a_upc(client):
    r = client.post("/api/lists/standard/items", json={"name": "Bananas"})
    assert r.status_code == 400 and r.json()["error"] == "Search your store and pick a product."


def test_push_and_prices_are_refused_on_a_hand_off_list(client, aldi, kroger, with_store):
    kroger.signed_in = True
    client.post(f"/api/lists/{aldi}/items", json={"name": "Bananas"})
    r = client.post(f"/api/lists/{aldi}/push", json=[{"upc": "bananas", "name": "Bananas", "quantity": 5}])
    assert r.status_code == 400 and r.json()["error"] == "This list is for Aldi; use Open at Aldi instead."
    assert client.get(f"/api/lists/{aldi}").json()["items"][0]["quantity"] == 1   # the body was not saved
    r = client.post(f"/api/lists/{aldi}/prices")
    assert r.status_code == 400 and r.json()["error"] == "This list is for Aldi; use Open at Aldi instead."
    assert kroger.cart_calls == [] and kroger.lookup_calls == []


def test_import_into_a_hand_off_list_keys_by_name_and_drops_kroger_fields(client, aldi):
    picks = [{"upc": "", "name": "Bananas", "quantity": 2},
             {"upc": "0001111041700", "name": "Kroger Milk", "size": "1 gal", "image": "https://img/1.jpg",
              "price": 3.49, "included": False, "quantity": 1},
             {"name": "bananas", "quantity": 1},
             {"name": "   "}]
    r = client.post(f"/api/lists/{aldi}/import", json=picks)
    assert r.status_code == 200
    rows = [(i["upc"], i["name"], i["quantity"], i["included"], i["size"], i["image"], i["price"]) for i in r.json()["items"]]
    assert rows == [("bananas", "Bananas", 3, True, "", None, None), ("kroger-milk", "Kroger Milk", 1, False, "", None, None)]


def test_import_into_a_kroger_list_requires_upcs(client):
    r = client.post("/api/lists/standard/import", json=[{"upc": "0001", "name": "Milk"}, {"name": "Bananas"}])
    assert r.status_code == 400 and r.json()["error"] == "Search your store and pick a product."
    assert client.get("/api/lists/standard").json()["items"] == []


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


# -- Aldi catalog -------------------------------------------------------------------------

def aldi_product(upc, name, **over):
    base = {"upc": upc, "name": name, "brand": "Friendly Farms", "size": "1 gal", "price": 2.57, "promo": None,
            "in_stock": True, "image": f"https://img/{upc}.jpg", "url": f"https://www.aldi.us/store/aldi/products/{upc[5:]}"}
    base.update(over)
    return base


class FakeAldi:
    def __init__(self):
        self.error: Exception | None = None
        self.searches, self.lookups = [], []
        self.accounts = {"v2.good": {"name": "Pat", "email": "pat@example.com"}}   # sid -> account
        self.cart_calls = []                 # (sid, [(upc, quantity)])
        self.unknown = set()                 # upcs fill_cart rejects
        self.cart_error: Exception | None = None
        self.catalog = {"aldi:16902710": aldi_product("aldi:16902710", "Whole Milk"),
                        "aldi:20957684": aldi_product("aldi:20957684", "2% Milk", price=2.85, promo=2.49)}

    def _check(self):
        if self.error:
            raise self.error

    def search_products(self, term, limit=10):
        self.searches.append(term)
        self._check()
        return [dict(p, name=f"{p['name']} for {term}") for p in self.catalog.values()][:limit]

    def lookup_products(self, ids):
        self._check()
        self.lookups.append(list(ids))
        return {i: self.catalog[i] for i in ids if i in self.catalog}

    def store(self):
        self._check()
        return {"store_id": "24032", "store_name": "ALDI - SPR 20 - Marysville", "address": "15740 US-36 E"}

    def account(self, sid):
        if sid not in self.accounts:
            raise AldiError(401, SIGNED_OUT)
        return self.accounts[sid]

    def fill_cart(self, sid, wanted):
        self.cart_calls.append((sid, list(wanted)))
        if self.cart_error:
            raise self.cart_error
        if sid not in self.accounts:
            raise AldiError(401, SIGNED_OUT)
        upcs = [u for u, _ in wanted]
        return CartResult(added=[u for u in upcs if u not in self.unknown], rejected=[u for u in upcs if u in self.unknown])


@pytest.fixture
def fake_aldi():
    return FakeAldi()


@pytest.fixture
def aldi_client(storage, kroger, fake_aldi):
    return TestClient(create_app(storage, kroger, aldi=fake_aldi), follow_redirects=False)


@pytest.fixture
def aldi_list(aldi_client):
    return aldi_client.post("/api/lists", json={"name": "Aldi run", "store": "aldi"}).json()["slug"]


def test_state_reports_aldi_catalog_when_configured(aldi_client):
    stores = {s["key"]: s for s in aldi_client.get("/api/state").json()["stores"]}
    assert stores["aldi"] == {"key": "aldi", "name": "Aldi", "catalog": True, "cart": False, "linked_cart": True}
    assert stores["kroger"]["linked_cart"] is False
    assert aldi_client.get("/api/state").json()["aldi_account"] is None


def test_state_without_an_aldi_client_has_no_linked_cart(client):
    stores = {s["key"]: s for s in client.get("/api/state").json()["stores"]}
    assert stores["aldi"]["linked_cart"] is False


def test_search_by_store_needs_no_kroger_store(aldi_client, fake_aldi):
    r = aldi_client.get("/api/search", params={"term": "milk", "store": "aldi"})
    assert r.status_code == 200
    assert [p["upc"] for p in r.json()["results"]] == ["aldi:16902710", "aldi:20957684"]
    assert fake_aldi.searches == ["milk"]
    assert aldi_client.get("/api/search", params={"term": "milk"}).json()["error"] == "Pick a store first."
    r = aldi_client.get("/api/search", params={"term": "milk", "store": "meijer"})
    assert r.status_code == 400 and r.json()["error"] == "Meijer lists have no catalog; add items by name."


def test_aldi_unconfigured_has_no_catalog(client):
    r = client.get("/api/search", params={"term": "milk", "store": "aldi"})
    assert r.status_code == 400 and r.json()["error"] == "Aldi lists have no catalog; add items by name."
    assert client.get("/api/aldi/store").json()["error"] == "Aldi isn't configured."


def test_aldi_store(aldi_client):
    assert aldi_client.get("/api/aldi/store").json()["store"]["store_name"] == "ALDI - SPR 20 - Marysville"


def test_aldi_errors_are_502_with_the_message(aldi_client, fake_aldi):
    fake_aldi.error = AldiError(502, CHANGED)
    r = aldi_client.get("/api/search", params={"term": "milk", "store": "aldi"})
    assert r.status_code == 502 and r.json()["error"] == CHANGED
    r = aldi_client.post("/api/import/resolve", json={"names": ["milk", "eggs"], "store": "aldi"})
    assert r.status_code == 502 and r.json()["error"] == f"Couldn't look up any items: {CHANGED}"


def test_resolve_against_aldi(aldi_client):
    r = aldi_client.post("/api/import/resolve", json={"names": ["milk"], "store": "aldi"})
    assert r.status_code == 200
    (match,) = r.json()["matches"]
    assert match["name"] == "milk" and [c["upc"] for c in match["candidates"]] == ["aldi:16902710", "aldi:20957684"]


def test_aldi_list_takes_products_and_plain_items(aldi_client, aldi_list):
    milk = aldi_product("aldi:16902710", "Whole Milk")
    r = aldi_client.post(f"/api/lists/{aldi_list}/items", json={k: milk[k] for k in ("upc", "name", "size", "image", "price", "promo")})
    r = aldi_client.post(f"/api/lists/{aldi_list}/items", json={"name": "Birthday candles"})
    r = aldi_client.post(f"/api/lists/{aldi_list}/items", json={"upc": "aldi:16902710", "name": "Whole Milk"})  # again: one more
    r = aldi_client.post(f"/api/lists/{aldi_list}/items", json={"upc": "0001111041700", "name": "Kroger Milk"})  # not Aldi's: plain
    items = r.json()["items"]
    assert [(i["upc"], i["name"], i["size"], i["price"], i["quantity"]) for i in items] == [
        ("aldi:16902710", "Whole Milk", "1 gal", 2.57, 2), ("birthday-candles", "Birthday candles", "", None, 1),
        ("kroger-milk", "Kroger Milk", "", None, 1)]
    assert items[0]["price_checked_at"]


def test_import_into_aldi_keeps_catalog_products_and_plain_rows(aldi_client, aldi_list):
    picks = [{"upc": "aldi:16902710", "name": "Whole Milk", "size": "1 gal", "price": 2.57, "image": "https://img/m.jpg", "quantity": 2},
             {"upc": "", "name": "Birthday candles"},
             {"upc": "aldi:16902710", "name": "Whole Milk", "quantity": 1},
             {"upc": "0001111041700", "name": "Kroger Milk", "size": "1 gal", "price": 3.49}]   # a Kroger UPC: plain here
    items = aldi_client.post(f"/api/lists/{aldi_list}/import", json=picks).json()["items"]
    assert [(i["upc"], i["quantity"], i["size"], i["image"], i["price"]) for i in items] == [
        ("aldi:16902710", 3, "1 gal", "https://img/m.jpg", 2.57), ("birthday-candles", 1, "", None, None),
        ("kroger-milk", 1, "", None, None)]


def test_aldi_prices_batch_lookup_and_leave_plain_items(aldi_client, aldi_list, fake_aldi):
    aldi_client.put(f"/api/lists/{aldi_list}/items", json=[
        {"upc": "aldi:20957684", "name": "2% Milk", "price": 3.10},
        {"upc": "aldi:99999", "name": "Gone"},
        {"upc": "candles", "name": "Candles"}])
    r = aldi_client.post(f"/api/lists/{aldi_list}/prices")
    assert r.status_code == 200 and r.json()["failed"] == 1
    assert fake_aldi.lookups == [["aldi:20957684", "aldi:99999"]]
    milk, gone, candles = r.json()["items"]
    assert (milk["price"], milk["promo"]) == (2.85, 2.49) and milk["price_checked_at"]
    assert gone["price"] is None and candles["price_checked_at"] is None
    fake_aldi.error = AldiError(0, "Couldn't reach aldi.us: ConnectError")
    r = aldi_client.post(f"/api/lists/{aldi_list}/prices")
    assert r.status_code == 502 and r.json()["error"] == "Couldn't check prices: Couldn't reach aldi.us: ConnectError"


def test_aldi_price_refresh_keeps_a_price_it_cannot_read(aldi_client, aldi_list, fake_aldi):
    aldi_client.put(f"/api/lists/{aldi_list}/items", json=[{"upc": "aldi:16902710", "name": "Whole Milk", "price": 2.57}])
    fake_aldi.catalog["aldi:16902710"].update(price=None, promo=None)
    item = aldi_client.post(f"/api/lists/{aldi_list}/prices").json()["items"][0]
    assert item["price"] == 2.57 and item["price_checked_at"] is None


def test_aldi_resolve_stops_early_once_the_site_is_down(aldi_client, fake_aldi):
    fake_aldi.error = AldiError(0, "Couldn't reach aldi.us: ConnectError")
    r = aldi_client.post("/api/import/resolve", json={"names": [f"item {n}" for n in range(12)], "store": "aldi"})
    assert r.status_code == 502
    assert r.json()["error"] == "Couldn't look up any items: Couldn't reach aldi.us: ConnectError"
    assert len(fake_aldi.searches) <= 4   # at most one wave of the thread pool; the rest skip


def test_aldi_list_hands_off_with_product_pages(aldi_client, aldi_list, kroger):
    kroger.signed_in = True
    aldi_client.put(f"/api/lists/{aldi_list}/items", json=[
        {"upc": "aldi:16902710", "name": "Whole Milk"}, {"upc": "candles", "name": "Candles"}])
    urls = [i["url"] for i in aldi_client.get(f"/api/lists/{aldi_list}/handoff").json()["items"]]
    assert urls == ["https://www.aldi.us/store/aldi/products/16902710", "https://www.aldi.us/store/aldi/s?k=Candles"]


def test_handoff_on_a_kroger_list_is_400(client):
    r = client.get("/api/lists/standard/handoff")
    assert r.status_code == 400 and r.json()["error"] == "Kroger lists are sent to the cart instead."


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
    assert r.json() == {"lists": [{"slug": "other", "name": "Other", "count": 0, "store": "kroger"}], "active_list": "other"}


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


def test_rename_corrupt_list_is_refused_and_file_untouched(client, tmp_path):
    path = tmp_path / "lists" / "standard.json"
    path.write_text("nope", encoding="utf-8")
    r = client.patch("/api/lists/standard", json={"name": "Weekly"})
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["error"]
    assert path.read_text(encoding="utf-8") == "nope"


def test_duplicate_corrupt_list_is_refused(client, tmp_path, storage):
    path = tmp_path / "lists" / "standard.json"
    path.write_text("nope", encoding="utf-8")
    r = client.post("/api/lists/standard/duplicate", json={"name": "Party"})
    assert r.status_code == 400
    assert "unreadable" in r.json()["error"]
    assert path.read_text(encoding="utf-8") == "nope"
    assert storage.list_slugs() == ["standard"]


def test_slug_traversal_is_404(client, tmp_path, storage):
    before = (tmp_path / "config.json").read_text(encoding="utf-8")
    r = client.put("/api/active", json={"slug": "../config"})
    assert r.status_code == 404
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == before
    assert storage.active_list() == "standard"


def test_delete_non_active_list_keeps_active_route(client):
    client.post("/api/lists", json={"name": "Other"})
    r = client.delete("/api/lists/other")
    assert r.status_code == 200
    assert r.json() == {"lists": [{"slug": "standard", "name": "Standard", "count": 0, "store": "kroger"}], "active_list": "standard"}
    assert client.get("/api/lists/standard").status_code == 200


# -- items --------------------------------------------------------------------------

def test_add_item_stores_image_and_dedupes(client):
    r = client.post("/api/lists/standard/items", json={**MILK, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99})
    [milk] = r.json()["items"]
    assert milk.pop("price_checked_at")  # a priced add is stamped: the price was fetched just now
    expected = {**MILK, **FULL, "image": "https://img/1.jpg", "price": 3.49, "promo": 2.99}
    expected.pop("price_checked_at")
    assert milk == expected
    client.post("/api/lists/standard/items", json=MILK)
    assert len(client.get("/api/lists/standard").json()["items"]) == 1


def test_add_item_without_price_is_not_stamped(client):
    r = client.post("/api/lists/standard/items", json=EGGS)
    assert r.json()["items"] == [{**EGGS, **FULL}]


def test_null_size_from_kroger_is_accepted_and_stored_as_empty(client, storage):
    r = client.post("/api/lists/standard/items", json={"upc": "0003", "name": "Bread", "size": None})
    assert r.status_code == 200
    r = client.post("/api/lists/standard/import", json=[{"upc": "0004", "name": "Jam", "size": None, "image": None}])
    assert r.status_code == 200
    assert [i.size for i in storage.load_list("standard").items] == ["", ""]


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


def test_push_records_an_order_with_store_and_total(client, kroger, storage, with_store):
    body = [{**MILK, **FULL, "quantity": 2, "price": 3.49, "promo": 2.99}, {**EGGS, **FULL, "included": False}]
    r = client.post("/api/lists/standard/push", json=body)
    assert r.status_code == 200
    order = r.json()["order"]
    assert r.json()["warning"] is None
    assert order["list_slug"] == "standard" and order["list_name"] == "Standard"
    assert order["store_id"] == "01400943" and order["store_name"] == "Kroger Hyde Park"
    assert order["item_count"] == 1 and order["estimated_total"] == 5.98
    assert [i["upc"] for i in order["items"]] == ["0001"]
    assert [o["id"] for o in storage.list_orders()] == [order["id"]]


def test_rejected_push_records_nothing(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert storage.list_orders() == []


def test_push_still_succeeds_when_history_cannot_be_written(client, storage, monkeypatch):
    def boom(order):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "record_order", boom)
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.status_code == 200
    assert r.json()["order"] is None
    assert "order history" in r.json()["warning"]


# -- orders ------------------------------------------------------------------------------

def push_one(client, **over):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL, "price": 3.49, **over}])
    assert r.status_code == 200
    return r.json()["order"]


def test_list_orders_newest_first_with_full_items(client, kroger):
    first = push_one(client)
    second = push_one(client, quantity=3)
    r = client.get("/api/orders")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert [o["id"] for o in orders] == [second["id"], first["id"]]
    assert orders[0]["items"][0]["quantity"] == 3


def test_delete_order(client):
    order = push_one(client)
    assert client.delete(f"/api/orders/{order['id']}").status_code == 204
    assert client.get("/api/orders").json()["orders"] == []
    r = client.delete(f"/api/orders/{order['id']}")
    assert r.status_code == 404
    assert "No order" in r.json()["error"]


def test_reorder_pushes_snapshot_and_records_with_source(client, kroger, with_store):
    source = push_one(client, quantity=2)
    kroger.cart_calls.clear()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    new = r.json()["order"]
    assert new["reorder_of"] == source["id"]
    assert new["list_slug"] == "standard" and new["list_name"] == "Standard"
    assert new["item_count"] == 1 and new["estimated_total"] == 6.98
    assert [o["id"] for o in client.get("/api/orders").json()["orders"]] == [new["id"], source["id"]]


def test_reorder_unknown_is_404(client):
    r = client.post("/api/orders/nope/reorder")
    assert r.status_code == 404
    assert r.json() == {"error": "No order with id 'nope'."}


def test_reorder_with_nothing_sendable_is_400(client, storage):
    storage.record_order({"id": "empty", "placed_at": "2026-10-26T00:00:00+00:00", "items": [{"upc": "0001", "quantity": 0}]})
    r = client.post("/api/orders/empty/reorder")
    assert r.status_code == 400
    assert r.json()["error"] == "Nothing to send."


def test_reorder_kroger_rejection_flags_upcs_and_records_nothing(client, kroger, storage):
    source = push_one(client)
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001")
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0001", "bad_upcs": ["0001"]}
    assert [o["id"] for o in storage.list_orders()] == [source["id"]]


def test_reorder_when_not_signed_in_is_401(client, kroger):
    source = push_one(client)
    kroger.cart_error = NotSignedIn()
    r = client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 401


# -- usage and photo suggestions -----------------------------------------------------------

def test_usage_profile_from_recorded_orders(client):
    push_one(client, quantity=2)
    push_one(client, quantity=4)
    (row,) = client.get("/api/usage").json()["items"]
    assert row["upc"] == "0001" and row["name"] == "Whole Milk" and row["times"] == 2
    assert row["avg_quantity"] == 3.0 and row["days_since_last"] == 0


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def photo_body(n=1, note="out of eggs", media_type="image/png", data=PNG, slug="standard"):
    return {"photos": [{"media_type": media_type, "data": data}] * n, "slug": slug, "note": note}


@pytest.fixture
def analyzer_client(storage, kroger):
    calls = []

    def fake(paths, note, list_name, items, profile, *, model, timeout):
        calls.append({"paths": [p for p in paths], "exists": [p.exists() for p in paths], "note": note,
                      "list_name": list_name, "items": list(items), "profile": profile, "model": model,
                      "timeout": timeout})
        return [Suggestion("whole milk", 1, "low", "Half left", "Dairy"), Suggestion("large eggs", 2, "stocked", "")]
    app = create_app(storage, kroger, photo_model="test-model", photo_timeout=7, analyzer=fake)
    client = TestClient(app, follow_redirects=False)
    client.calls = calls
    return client


def test_photo_suggest_writes_temp_files_passes_context_and_returns_lines(analyzer_client, storage):
    push_one(analyzer_client)  # records an order (usage profile) and leaves Whole Milk on the list
    storage.save_list("standard", "Standard", [Item(upc="0002", name="Eggs", size="12 ct", quantity=1)])
    r = analyzer_client.post("/api/photo/suggest", json=photo_body(n=2))
    assert r.status_code == 200
    assert r.json()["lines"] == [
        {"raw": "low: Half left", "name": "whole milk", "quantity": 1, "exact": None, "kind": "low", "reason": "Half left", "group": "Dairy"},
        {"raw": "stocked", "name": "large eggs", "quantity": 2, "exact": None, "kind": "stocked", "reason": "", "group": ""},
    ]
    (call,) = analyzer_client.calls
    assert [p.suffix for p in call["paths"]] == [".png", ".png"] and call["exists"] == [True, True]
    assert not any(p.exists() for p in call["paths"]), "photos must be deleted after the request"
    assert call["note"] == "out of eggs" and "Whole Milk: bought 1x" in call["profile"]
    assert call["list_name"] == "Standard" and [i.name for i in call["items"]] == ["Eggs"]
    assert call["model"] == "test-model" and call["timeout"] == 7


def test_photo_suggest_unknown_list_is_404_before_analysis(analyzer_client):
    r = analyzer_client.post("/api/photo/suggest", json=photo_body(slug="nope"))
    assert r.status_code == 404 and analyzer_client.calls == []


def test_photo_suggest_validation(analyzer_client):
    c = analyzer_client
    assert c.post("/api/photo/suggest", json=photo_body(n=0)).status_code == 400
    assert c.post("/api/photo/suggest", json=photo_body(n=5)).status_code == 413
    assert c.post("/api/photo/suggest", json=photo_body(media_type="image/gif")).status_code == 415
    assert c.post("/api/photo/suggest", json=photo_body(data="not base64!")).status_code == 400
    big = base64.b64encode(b"x" * (6 * 1024 * 1024 + 1)).decode()
    assert c.post("/api/photo/suggest", json=photo_body(data=big)).status_code == 413
    assert c.calls == []


def test_photo_suggest_maps_vision_errors(storage, kroger):
    def broken(paths, note, list_name, items, profile, *, model, timeout):
        raise VisionError(503, "Claude Code isn't installed on the computer running the app, so photos can't be analyzed.")
    client = TestClient(create_app(storage, kroger, analyzer=broken))
    r = client.post("/api/photo/suggest", json=photo_body())
    assert r.status_code == 503 and "Claude Code isn't installed" in r.json()["error"]


def test_photo_suggest_with_no_groceries_is_422(storage, kroger):
    client = TestClient(create_app(storage, kroger, analyzer=lambda *a, **k: []))
    assert client.post("/api/photo/suggest", json=photo_body()).status_code == 422


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
    assert all(i["price_checked_at"].endswith("+00:00") for i in items)  # UTC, never naive local time
    assert items[0]["image"] == "https://img/0001.jpg"
    assert r.json()["failed"] == 0


def test_refresh_prices_partial_failure_keeps_old_values(client, kroger, with_store):
    # Seed via PUT so milk carries an old price with no stamp (a priced POST would stamp it).
    client.put("/api/lists/standard/items", json=[{**MILK, **FULL, "price": 1.0}, {**EGGS, **FULL}])
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


def test_refresh_prices_merges_into_latest_list_state(client, kroger, storage, with_store):
    # The lookups are slow; the user may edit the list meanwhile. The refresh must not revert those edits.
    client.post("/api/lists/standard/items", json=MILK)
    client.post("/api/lists/standard/items", json=EGGS)

    def lookup(upc, location_id):
        if upc == "0001":  # mid-refresh: user unchecks milk and removes eggs
            storage.save_list("standard", "Standard", [Item(**MILK, included=False)])
        return product(upc, f"Looked up {upc}", price=5.0, promo=4.0)
    kroger.lookup_product = lookup

    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 200
    [milk] = r.json()["items"]
    assert milk["included"] is False
    assert milk["price"] == 5.0 and milk["price_checked_at"]
    assert r.json()["failed"] == 0


def test_refresh_prices_stops_after_three_transport_failures(client, kroger, with_store):
    for n in range(6):
        client.post("/api/lists/standard/items", json={"upc": f"000{n}", "name": f"Item {n}"})
        kroger.lookups[f"000{n}"] = KrogerError(0, "Could not reach Kroger: ConnectError")
    r = client.post("/api/lists/standard/prices")
    assert r.status_code == 502
    assert "Could not reach Kroger" in r.json()["error"]
    assert len(kroger.lookup_calls) == 3  # transport failures aren't item-specific; stop hammering


# -- import ------------------------------------------------------------------------------

def test_import_parse_returns_lines(client):
    r = client.post("/api/import/parse", json={"text": "- 2x milk\n- eggs"})
    assert r.json() == {"lines": [
        {"raw": "- 2x milk", "name": "milk", "quantity": 2, "exact": None},
        {"raw": "- eggs", "name": "eggs", "quantity": 1, "exact": None},
    ]}


def test_import_parse_exported_csv_returns_exact_rows(client):
    text = "upc,name,size,quantity,included,price,promo,image\n0001,Whole Milk,1 gal,2,false,3.49,,\n"
    (line,) = client.post("/api/import/parse", json={"text": text}).json()["lines"]
    assert line["name"] == "Whole Milk" and line["quantity"] == 2
    assert line["exact"] == {"upc": "0001", "size": "1 gal", "included": False, "price": 3.49, "promo": None, "image": None}


def test_import_commit_honours_included_and_prices_for_new_items(client):
    body = [{"upc": "0002", "name": "Eggs", "size": "12 ct", "image": None, "quantity": 1,
             "included": False, "price": 4.0, "promo": 3.5}]
    items = client.post("/api/lists/standard/import", json=body).json()["items"]
    assert [(i["upc"], i["included"], i["price"], i["promo"], i["price_checked_at"]) for i in items] == \
        [("0002", False, 4.0, 3.5, None)]


# -- export ------------------------------------------------------------------------------

def test_export_csv_round_trips_through_import(client, storage):
    storage.save_list("standard", "Standard", [
        Item(upc="0001", name="Whole Milk, Vitamin D", size="1 gal", quantity=2, included=True,
             image="https://img/1.jpg", price=3.49, promo=2.99, price_checked_at="2026-10-26T00:00:00+00:00"),
        Item(upc="0002", name="Eggs", size="", quantity=1, included=False),
    ])
    r = client.get("/api/lists/standard/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="standard.csv"'
    text = r.content.decode("utf-8-sig")
    assert text.splitlines() == [
        "upc,name,size,quantity,included,price,promo,image",
        '0001,"Whole Milk, Vitamin D",1 gal,2,true,3.49,2.99,https://img/1.jpg',
        "0002,Eggs,,1,false,,,",
    ]
    # The exported file re-imports into a fresh list exactly, no search needed.
    client.post("/api/lists", json={"name": "Copy"})
    lines = client.post("/api/import/parse", json={"text": r.content.decode("utf-8")}).json()["lines"]
    picks = [{**l["exact"], "name": l["name"], "quantity": l["quantity"]} for l in lines]
    items = client.post("/api/lists/copy/import", json=picks).json()["items"]
    assert [(i["upc"], i["name"], i["size"], i["quantity"], i["included"], i["price"], i["promo"], i["image"])
            for i in items] == [
        ("0001", "Whole Milk, Vitamin D", "1 gal", 2, True, 3.49, 2.99, "https://img/1.jpg"),
        ("0002", "Eggs", "", 1, False, None, None, None),
    ]


def test_export_missing_list_is_404(client):
    assert client.get("/api/lists/nope/export.csv").status_code == 404


def test_export_empty_list_is_header_only(client):
    text = client.get("/api/lists/standard/export.csv").content.decode("utf-8-sig")
    assert text.splitlines() == ["upc,name,size,quantity,included,price,promo,image"]


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


def test_import_resolve_total_failure_is_502(client, with_store, kroger):
    def boom(term, location_id):
        raise KrogerError(0, "Could not reach Kroger: ConnectError")
    kroger.search_products = boom
    r = client.post("/api/import/resolve", json={"names": ["milk", "eggs"]})
    assert r.status_code == 502
    assert "Could not reach Kroger" in r.json()["error"]


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


# -- recipe suggestions ---------------------------------------------------------------------

RECIPE_HTML = ('<html><head><title>Site</title><script type="application/ld+json">'
               '{"@type":"Recipe","name":"Tikka Masala","recipeYield":"4","recipeIngredient":["2 lbs chicken","1 cup yogurt"]}'
               '</script></head><body></body></html>')


@pytest.fixture
def recipe_client(storage, kroger):
    calls = {"fetch": [], "suggest": []}

    def fetch(url):
        calls["fetch"].append(url)
        return Page(url=url, html=RECIPE_HTML)

    def suggester(page, note, list_name, items, profile, *, model, timeout):
        calls["suggest"].append({"page": page, "note": note, "list_name": list_name, "items": list(items),
                                 "profile": profile, "model": model, "timeout": timeout})
        return [Suggestion("chicken thighs", 2, "needed", "Two pounds", "Protein"), Suggestion("large eggs", 1, "on_list", "already on your list: Eggs ×1")]
    app = create_app(storage, kroger, photo_model="test-model", photo_timeout=7, page_fetcher=fetch,
                     recipe_suggester=suggester)
    client = TestClient(app, follow_redirects=False)
    client.calls = calls
    return client


def test_recipe_suggest_fetches_extracts_and_passes_list_items(recipe_client, storage):
    push_one(recipe_client)  # records an order (usage profile) but also overwrites the list with Whole Milk
    storage.save_list("standard", "Standard", [Item(upc="0002", name="Eggs", size="12 ct", quantity=1)])
    r = recipe_client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard", "note": "double it"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Tikka Masala"
    assert r.json()["lines"] == [
        {"raw": "needed: Two pounds", "name": "chicken thighs", "quantity": 2, "exact": None, "kind": "needed", "reason": "Two pounds", "group": "Protein"},
        {"raw": "on_list: already on your list: Eggs ×1", "name": "large eggs", "quantity": 1, "exact": None, "kind": "on_list", "reason": "already on your list: Eggs ×1", "group": ""},
    ]
    assert recipe_client.calls["fetch"] == ["https://site.test/r"]
    (call,) = recipe_client.calls["suggest"]
    assert isinstance(call["page"], Extracted) and call["page"].ingredients == ["2 lbs chicken", "1 cup yogurt"]
    assert call["note"] == "double it" and call["list_name"] == "Standard"
    assert [i.name for i in call["items"]] == ["Eggs"]
    assert "Whole Milk" in call["profile"] and call["model"] == "test-model" and call["timeout"] == 7


def test_recipe_suggest_unknown_list_is_404(recipe_client):
    r = recipe_client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "nope"})
    assert r.status_code == 404 and not recipe_client.calls["fetch"]


def test_recipe_suggest_passes_fetch_and_claude_errors_through(storage, kroger):
    def refused(url):
        raise RecipeError(502, "That site refused the request (HTTP 403). Copy the ingredients from the page and use Import instead.")
    client = TestClient(create_app(storage, kroger, page_fetcher=refused))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 502 and r.json()["error"].startswith("That site refused the request (HTTP 403)")

    def broken(*a, **k):
        raise VisionError(503, "Photos can't be analyzed: set ANTHROPIC_API_KEY in .env, or install Claude Code on the computer running the app.")
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, RECIPE_HTML), recipe_suggester=broken))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 503


def test_recipe_suggest_without_recipe_on_page_is_422(storage, kroger):
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, "<html><body>nothing</body></html>"),
                                   recipe_suggester=lambda *a, **k: []))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 422 and r.json()["error"].startswith("Couldn't find a recipe on that page.")


def test_recipe_suggest_with_no_suggestions_is_422(storage, kroger):
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, RECIPE_HTML),
                                   recipe_suggester=lambda *a, **k: []))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 422 and r.json()["error"] == "Claude couldn't find any ingredients on that page."


# -- the Aldi cart (v7.2) ---------------------------------------------------------------------

@pytest.fixture
def connected(aldi_client):
    r = aldi_client.put("/api/aldi/account", json={"sid": "v2.good"})
    assert r.status_code == 200
    return r.json()["account"]


def test_connect_aldi_account_stores_the_cookie_but_never_shows_it(aldi_client, storage, connected):
    assert connected["name"] == "Pat" and connected["email"] == "pat@example.com" and connected["connected_at"]
    assert "sid" not in connected
    assert storage.load_aldi_session()["sid"] == "v2.good"
    assert aldi_client.get("/api/aldi/account").json()["account"] == connected
    state = aldi_client.get("/api/state").json()
    assert state["aldi_account"] == connected and "v2.good" not in json.dumps(state)


@pytest.mark.parametrize("pasted", ['  "v2.good"  ', "__Host-instacart_sid=v2.good", "__Host-instacart_sid=v2.good;"])
def test_connect_tolerates_a_pasted_name_quotes_and_semicolon(aldi_client, storage, pasted):
    assert aldi_client.put("/api/aldi/account", json={"sid": pasted}).status_code == 200
    assert storage.load_aldi_session()["sid"] == "v2.good"


def test_connect_rejects_empty_and_guest_cookies(aldi_client, storage):
    r = aldi_client.put("/api/aldi/account", json={"sid": "  "})
    assert r.status_code == 400 and r.json()["error"] == "Paste the __Host-instacart_sid cookie value."
    r = aldi_client.put("/api/aldi/account", json={"sid": "v2.guest"})
    assert r.status_code == 400
    assert r.json()["error"] == "That cookie isn't signed in to aldi.us. Log in at aldi.us first, then copy it again."
    assert storage.load_aldi_session() is None


@pytest.mark.parametrize("pasted", ["v2.good\u2026", "v2.go od", "v2.good\nmore", "a=1; b=2"])
def test_connect_refuses_something_that_isnt_a_cookie_value(aldi_client, storage, pasted):
    r = aldi_client.put("/api/aldi/account", json={"sid": pasted})
    assert r.status_code == 400 and r.json()["error"] == "That doesn't look like a cookie value; copy it again."
    assert storage.load_aldi_session() is None


def test_connect_when_aldi_us_is_unreachable_saves_nothing(aldi_client, storage, fake_aldi):
    fake_aldi.accounts = None   # account() below raises before looking anything up
    fake_aldi.error = AldiError(0, "Couldn't reach aldi.us: ConnectError")
    fake_aldi.account = lambda sid: fake_aldi._check()
    r = aldi_client.put("/api/aldi/account", json={"sid": "v2.good"})
    assert r.status_code == 502 and r.json()["error"] == "Couldn't reach aldi.us: ConnectError"
    assert storage.load_aldi_session() is None


def test_connect_without_an_aldi_client(client):
    r = client.put("/api/aldi/account", json={"sid": "v2.good"})
    assert r.status_code == 400 and r.json()["error"] == "Aldi isn't configured."


def test_disconnect_forgets_the_cookie(aldi_client, storage, connected):
    assert aldi_client.delete("/api/aldi/account").status_code == 204
    assert storage.load_aldi_session() is None
    assert aldi_client.get("/api/aldi/account").json()["account"] is None


MILK_ALDI = {"upc": "aldi:16902710", "name": "Whole Milk", "size": "1 gal", "price": 2.57, "quantity": 2}
EGGS_ALDI = {"upc": "aldi:20957684", "name": "Eggs", "size": "12 ct", "price": 1.99}
CANDLES = {"upc": "candles", "name": "Candles", "size": ""}


def test_push_aldi_needs_a_connected_cart_and_changes_nothing_without_one(aldi_client, aldi_list, fake_aldi):
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI])
    assert r.status_code == 400 and r.json() == {"error": "Connect your Aldi cart first.", "aldi_signed_in": False}
    assert aldi_client.get(f"/api/lists/{aldi_list}").json()["items"] == [] and fake_aldi.cart_calls == []


def test_push_aldi_sends_checked_products_and_skips_plain_items(aldi_client, aldi_list, fake_aldi, storage, connected):
    body = [MILK_ALDI, {**EGGS_ALDI, "included": False}, CANDLES]
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=body)
    assert r.status_code == 200
    assert fake_aldi.cart_calls == [("v2.good", [("aldi:16902710", 2)])]
    data = r.json()
    assert [i["upc"] for i in data["added"]] == ["aldi:16902710"]
    assert data["skipped"] == ["Candles"] and data["bad_upcs"] == []
    assert [i.upc for i in storage.load_list(aldi_list).items] == ["aldi:16902710", "aldi:20957684", "candles"]
    order = data["order"]
    assert order["cart"] == "aldi" and order["store_name"] == "Aldi" and order["estimated_total"] == 5.14
    assert [o["id"] for o in storage.list_orders()] == [order["id"]]


def test_push_aldi_with_only_plain_items_checked(aldi_client, aldi_list, fake_aldi, connected):
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[CANDLES])
    assert r.status_code == 400
    assert r.json()["error"] == "Nothing checked is an Aldi product; use Open at Aldi for items added as typed."
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[{**MILK_ALDI, "included": False}])
    assert r.status_code == 400 and r.json()["error"] == "Nothing is checked to send."
    assert fake_aldi.cart_calls == []


def test_push_aldi_reports_rejected_products(aldi_client, aldi_list, fake_aldi, storage, connected):
    fake_aldi.unknown = {"aldi:20957684"}
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI, EGGS_ALDI])
    assert r.status_code == 200
    assert [i["upc"] for i in r.json()["added"]] == ["aldi:16902710"] and r.json()["bad_upcs"] == ["aldi:20957684"]
    assert [i["upc"] for i in storage.list_orders()[0]["items"]] == ["aldi:16902710"]
    fake_aldi.unknown = {"aldi:16902710"}
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI])
    assert r.status_code == 502
    assert r.json() == {"error": "Aldi wouldn't add any of these items.", "bad_upcs": ["aldi:16902710"]}
    assert len(storage.list_orders()) == 1


def test_push_aldi_signed_out_forgets_the_cookie(aldi_client, aldi_list, fake_aldi, storage, connected):
    fake_aldi.accounts.clear()
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI])
    assert r.status_code == 401 and r.json() == {"error": SIGNED_OUT, "aldi_signed_in": False}
    assert storage.load_aldi_session() is None and storage.list_orders() == []


def test_push_aldi_site_errors_are_502(aldi_client, aldi_list, fake_aldi, connected):
    fake_aldi.cart_error = AldiError(502, CHANGED)
    r = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI])
    assert r.status_code == 502 and r.json()["error"] == CHANGED


def test_push_on_meijer_is_still_refused(aldi_client, connected):
    slug = aldi_client.post("/api/lists", json={"name": "Meijer run", "store": "meijer"}).json()["slug"]
    r = aldi_client.post(f"/api/lists/{slug}/push", json=[CANDLES])
    assert r.status_code == 400 and r.json()["error"] == "This list is for Meijer; use Open at Meijer instead."


def test_push_aldi_without_an_aldi_client_is_refused(client, aldi):
    r = client.post(f"/api/lists/{aldi}/push", json=[MILK_ALDI])
    assert r.status_code == 400 and r.json()["error"] == "This list is for Aldi; use Open at Aldi instead."


def test_reorder_an_aldi_order_goes_to_the_aldi_cart(aldi_client, aldi_list, fake_aldi, kroger, connected):
    source = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI]).json()["order"]
    r = aldi_client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 200
    assert fake_aldi.cart_calls[-1] == ("v2.good", [("aldi:16902710", 2)]) and kroger.cart_calls == []
    assert r.json()["order"]["cart"] == "aldi" and r.json()["order"]["reorder_of"] == source["id"]
    aldi_client.delete("/api/aldi/account")
    r = aldi_client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 400 and r.json()["error"] == "Connect your Aldi cart first."


def test_kroger_orders_say_so(client, kroger):
    r = client.post("/api/lists/standard/push", json=[{**MILK, **FULL}])
    assert r.json()["order"]["cart"] == "kroger"


def test_reorder_an_aldi_order_reports_rejections_and_sign_out(aldi_client, aldi_list, fake_aldi, storage, connected):
    source = aldi_client.post(f"/api/lists/{aldi_list}/push", json=[MILK_ALDI, EGGS_ALDI]).json()["order"]
    fake_aldi.unknown = {"aldi:20957684"}
    r = aldi_client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 200 and r.json()["bad_upcs"] == ["aldi:20957684"]
    fake_aldi.unknown = {"aldi:16902710", "aldi:20957684"}
    r = aldi_client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 502 and r.json()["error"] == "Aldi wouldn't add any of these items."
    fake_aldi.accounts.clear()
    r = aldi_client.post(f"/api/orders/{source['id']}/reorder")
    assert r.status_code == 401 and r.json()["aldi_signed_in"] is False
    assert storage.load_aldi_session() is None and len(storage.list_orders()) == 2


def test_reorder_an_aldi_order_without_an_aldi_client(client, storage):
    storage.record_order({"id": "a1", "placed_at": "2026-11-06T00:00:00+00:00", "cart": "aldi",
                          "items": [{"upc": "aldi:1", "quantity": 1}]})
    r = client.post("/api/orders/a1/reorder")
    assert r.status_code == 400 and r.json()["error"] == "Aldi isn't configured."
