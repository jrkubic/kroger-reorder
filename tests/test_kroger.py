import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.kroger import KrogerClient, KrogerError, NotSignedIn
from app.storage import Storage


class FakeKrogerServer:
    """Records every request and answers from a FIFO queue of (status, json_body)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list[tuple[int, dict | None]] = []

    def queue(self, status: int, body: dict | None = None) -> None:
        self.responses.append((status, body))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx.Response(status) if body is None else httpx.Response(status, json=body)


class FakeClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def server():
    return FakeKrogerServer()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path)


@pytest.fixture
def client(server, clock, storage):
    return KrogerClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/callback",
        storage=storage,
        transport=httpx.MockTransport(server.handler),
        clock=clock,
    )


def form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


TOKEN = {"access_token": "app-tok", "token_type": "bearer", "expires_in": 1800}


def test_app_token_uses_basic_auth_and_is_cached(client, server):
    server.queue(200, TOKEN)
    assert client.app_token() == "app-tok"
    assert client.app_token() == "app-tok"
    assert len(server.requests) == 1
    req = server.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/connect/oauth2/token"
    expected = "Basic " + base64.b64encode(b"cid:secret").decode()
    assert req.headers["Authorization"] == expected
    assert form(req) == {"grant_type": "client_credentials", "scope": "product.compact"}


def test_app_token_refetched_after_expiry(client, server, clock):
    server.queue(200, TOKEN)
    server.queue(200, {**TOKEN, "access_token": "app-tok-2"})
    client.app_token()
    clock.now += 1800
    assert client.app_token() == "app-tok-2"
    assert len(server.requests) == 2


def test_find_stores_parses_locations(client, server):
    server.queue(200, TOKEN)
    server.queue(200, {"data": [
        {"locationId": "01400943", "name": "Kroger Hyde Park",
         "address": {"addressLine1": "3760 Paxton Ave", "city": "Cincinnati"}},
        {"locationId": "01400001", "name": "Kroger Downtown", "address": {}},
    ]})
    stores = client.find_stores("45209")
    assert stores == [
        {"id": "01400943", "name": "Kroger Hyde Park", "address": "3760 Paxton Ave, Cincinnati"},
        {"id": "01400001", "name": "Kroger Downtown", "address": ""},
    ]
    req = server.requests[1]
    assert req.url.path == "/v1/locations"
    assert req.url.params["filter.zipCode.near"] == "45209"
    assert req.headers["Authorization"] == "Bearer app-tok"


def test_search_products_parses_and_prefers_promo_price(client, server):
    server.queue(200, TOKEN)
    server.queue(200, {"data": [
        {"upc": "0001111041700", "description": "Kroger Whole Milk", "brand": "Kroger",
         "items": [{"size": "1 gal", "price": {"regular": 3.49, "promo": 2.99}}]},
        {"upc": "0001111041701", "description": "Kroger 2% Milk", "brand": "Kroger",
         "items": [{"size": "1 gal", "price": {"regular": 3.49, "promo": 0}}]},
        {"upc": "0001111041702", "description": "Mystery Item", "items": []},
    ]})
    results = client.search_products("milk", "01400943")
    assert results == [
        {"upc": "0001111041700", "name": "Kroger Whole Milk", "brand": "Kroger", "size": "1 gal", "price": 2.99},
        {"upc": "0001111041701", "name": "Kroger 2% Milk", "brand": "Kroger", "size": "1 gal", "price": 3.49},
        {"upc": "0001111041702", "name": "Mystery Item", "brand": "", "size": "", "price": None},
    ]
    req = server.requests[1]
    assert req.url.path == "/v1/products"
    assert req.url.params["filter.term"] == "milk"
    assert req.url.params["filter.locationId"] == "01400943"


def test_kroger_error_message_is_parsed_from_body(client, server):
    server.queue(200, TOKEN)
    server.queue(400, {"errors": {"timestamp": 1, "code": "PRODUCT-2001-400", "reason": "filter.term is required"}})
    with pytest.raises(KrogerError) as info:
        client.search_products("", "01400943")
    assert info.value.status == 400
    assert info.value.message == "filter.term is required"


def test_kroger_error_falls_back_to_status_when_body_is_not_json(client, server):
    server.queue(200, TOKEN)
    server.queue(503, None)
    with pytest.raises(KrogerError) as info:
        client.find_stores("45209")
    assert info.value.status == 503
    assert "503" in info.value.message
