import pytest
from fastapi.testclient import TestClient

from app.kroger import KrogerError, NotSignedIn
from app.main import create_app
from app.storage import Storage


class FakeKroger:
    def __init__(self):
        self.signed_in = False
        self.exchanged_code = None
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

    def exchange_code(self, code):
        self.exchanged_code = code
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
    r = client.get("/callback", params={"code": "abc"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"
    assert kroger.exchanged_code == "abc"
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


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
