import pytest
from fastapi.testclient import TestClient

from app.kroger import KrogerError, NotSignedIn
from app.main import create_app
from app.storage import Storage


class FakeKroger:
    def __init__(self):
        self.signed_in = False
        self.exchanged_code = None
        self.exchanged_state = None
        self.cart_calls = []
        self.cart_error: Exception | None = None

    def is_signed_in(self):
        return self.signed_in

    def find_stores(self, zip_code):
        return [{"id": "01400943", "name": "Kroger Hyde Park", "address": "3760 Paxton Ave, Cincinnati"}]

    def search_products(self, term, location_id):
        return [{"upc": "0001", "name": f"{term} at {location_id}", "brand": "Kroger", "size": "1 gal", "price": 3.49}]

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


def test_initial_state(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json() == {"store": None, "signed_in": False, "items": [], "warning": None}


def test_state_includes_list_warning_when_file_is_corrupt(client, tmp_path):
    (tmp_path / "list.json").write_text("nope", encoding="utf-8")
    body = client.get("/api/state").json()
    assert body["items"] == []
    assert "not valid JSON" in body["warning"]


def test_find_stores(client):
    r = client.get("/api/stores", params={"zip": "45209"})
    assert r.status_code == 200
    assert r.json()["stores"][0]["id"] == "01400943"


def test_choose_store_persists_and_shows_in_state(client, storage):
    r = client.put("/api/store", json={"id": "01400943", "name": "Kroger Hyde Park"})
    assert r.status_code == 200
    assert storage.load_config() == {"store_id": "01400943", "store_name": "Kroger Hyde Park"}
    assert client.get("/api/state").json()["store"] == {"store_id": "01400943", "store_name": "Kroger Hyde Park"}


def test_login_redirects_to_kroger(client):
    r = client.get("/auth/login")
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.kroger.com/v1/connect/oauth2/authorize")


def test_callback_exchanges_code_and_redirects_home(client, kroger):
    r = client.get("/callback", params={"code": "abc", "state": "s"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"
    assert kroger.exchanged_code == "abc"
    assert kroger.exchanged_state == "s"
    assert client.get("/api/state").json()["signed_in"] is True


def test_search_requires_store(client):
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 400
    assert "store" in r.json()["error"].lower()


def test_search_uses_saved_store(client, storage):
    storage.save_config({"store_id": "01400943", "store_name": "Kroger Hyde Park"})
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 200
    assert r.json()["results"][0]["name"] == "milk at 01400943"


def test_kroger_error_becomes_502_with_message(client, storage, kroger):
    storage.save_config({"store_id": "01400943", "store_name": "x"})

    def boom(term, location_id):
        raise KrogerError(429, "rate limited")

    kroger.search_products = boom
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: rate limited"}


def test_transport_error_is_502_without_kroger_said_prefix(client, storage, kroger):
    storage.save_config({"store_id": "01400943", "store_name": "x"})

    def boom(term, location_id):
        raise KrogerError(0, "Could not reach Kroger: ConnectError")

    kroger.search_products = boom
    r = client.get("/api/search", params={"term": "milk"})
    assert r.status_code == 502
    assert r.json() == {"error": "Could not reach Kroger: ConnectError"}


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


MILK = {"upc": "0001", "name": "Whole Milk", "size": "1 gal"}
EGGS = {"upc": "0002", "name": "Eggs", "size": "12 ct"}


def test_add_item_appends_with_defaults_and_dedupes(client, storage):
    r = client.post("/api/items", json=MILK)
    assert r.status_code == 200
    assert r.json()["items"] == [{**MILK, "quantity": 1, "included": True}]
    client.post("/api/items", json=MILK)
    assert len(client.get("/api/state").json()["items"]) == 1


def test_replace_items_saves_quantities_and_flags(client, storage):
    body = [{**MILK, "quantity": 3, "included": False}, {**EGGS, "quantity": 1, "included": True}]
    r = client.put("/api/items", json=body)
    assert r.status_code == 200
    assert r.json()["items"] == body
    assert storage.load_list().items[0].quantity == 3


def test_delete_item(client):
    client.post("/api/items", json=MILK)
    client.post("/api/items", json=EGGS)
    r = client.delete("/api/items/0001")
    assert [i["upc"] for i in r.json()["items"]] == ["0002"]


def test_push_sends_only_included_items_with_positive_quantity(client, kroger, storage):
    kroger.signed_in = True
    body = [
        {**MILK, "quantity": 2, "included": True},
        {**EGGS, "quantity": 1, "included": False},
        {"upc": "0003", "name": "Bread", "size": "", "quantity": 0, "included": True},
    ]
    r = client.post("/api/push", json=body)
    assert r.status_code == 200
    assert kroger.cart_calls == [[("0001", 2)]]
    assert [i["upc"] for i in r.json()["added"]] == ["0001"]
    # push also persists the latest edits
    assert storage.load_list().items[1].included is False


def test_push_with_nothing_checked_is_400_and_calls_nothing(client, kroger):
    r = client.post("/api/push", json=[{**MILK, "quantity": 1, "included": False}])
    assert r.status_code == 400
    assert kroger.cart_calls == []


def test_push_when_not_signed_in_is_401(client, kroger):
    kroger.cart_error = NotSignedIn()
    r = client.post("/api/push", json=[{**MILK, "quantity": 1, "included": True}])
    assert r.status_code == 401
    assert r.json()["signed_in"] is False


def test_push_kroger_rejection_flags_bad_upc(client, kroger, storage):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0002")
    body = [{**MILK, "quantity": 1, "included": True}, {**EGGS, "quantity": 1, "included": True}]
    r = client.post("/api/push", json=body)
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0002", "bad_upcs": ["0002"]}
    # edits survive a Kroger failure
    saved = storage.load_list().items
    assert [i.upc for i in saved] == ["0001", "0002"]
    assert all(i.included is True for i in saved)


def test_push_bad_upc_does_not_match_partial_codes(client, kroger):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0001111041700")
    body = [
        {"upc": "0001111041700", "name": "Milk", "size": "", "quantity": 1, "included": True},
        {"upc": "1111041", "name": "Eggs", "size": "", "quantity": 1, "included": True},
    ]
    r = client.post("/api/push", json=body)
    assert r.status_code == 502
    assert r.json()["bad_upcs"] == ["0001111041700"]


def test_callback_denied_redirects_home_with_error(client, kroger):
    r = client.get("/callback", params={"error": "access_denied"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/?auth_error=access_denied"
    assert kroger.exchanged_code is None


def test_callback_exchange_failure_redirects_home_with_error(client, kroger):
    def boom(code, state):
        raise KrogerError(400, "invalid_grant")

    kroger.exchange_code = boom
    r = client.get("/callback", params={"code": "x"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/?auth_error=invalid_grant"


def test_push_empty_body_does_not_touch_corrupt_list(client, tmp_path):
    (tmp_path / "list.json").write_text("nope", encoding="utf-8")
    r = client.post("/api/push", json=[])
    assert r.status_code == 400
    assert (tmp_path / "list.json").read_text(encoding="utf-8") == "nope"


def test_delete_missing_upc_leaves_list_unchanged(client):
    client.post("/api/items", json=MILK)
    r = client.delete("/api/items/9999")
    assert r.status_code == 200
    assert r.json()["items"] == [{**MILK, "quantity": 1, "included": True}]


def test_validation_error_uses_error_shape(client):
    r = client.post("/api/items", json={"upc": "1"})
    assert r.status_code == 422
    assert "error" in r.json()
    assert "name" in r.json()["error"]
