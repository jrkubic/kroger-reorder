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
        self.responses: list[tuple[int, dict | None, str | None]] = []

    def queue(self, status: int, body: dict | None = None, text: str | None = None) -> None:
        self.responses.append((status, body, text))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, text = self.responses.pop(0)
        if text is not None:
            return httpx.Response(status, text=text)
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


def test_html_error_body_is_summarised_as_status(client, server):
    server.queue(200, TOKEN)
    server.queue(502, text="<html><body><h1>502 Bad Gateway</h1></body></html>")
    with pytest.raises(KrogerError) as info:
        client.find_stores("45209")
    assert info.value.status == 502
    assert info.value.message == "HTTP 502"


def test_network_error_becomes_kroger_error(clock, storage):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = KrogerClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/callback",
        storage=storage,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )
    with pytest.raises(KrogerError) as info:
        client.find_stores("45209")
    assert "Could not reach Kroger" in info.value.message
    assert "ConnectError" in info.value.message


USER_TOKEN = {"access_token": "user-tok", "refresh_token": "refresh-1", "expires_in": 1800, "token_type": "bearer"}


def test_authorize_url_has_scope_and_redirect(client):
    url = urlparse(client.authorize_url())
    assert url.scheme == "https"
    assert url.netloc == "api.kroger.com"
    assert url.path == "/v1/connect/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q.pop("state")  # present and non-empty; value is random per call
    assert q == {
        "scope": "cart.basic:write",
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://localhost:8000/callback",
    }


def state_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def test_exchange_code_saves_tokens_with_expiry(client, server, storage, clock):
    server.queue(200, USER_TOKEN)
    state = state_from(client.authorize_url())
    client.exchange_code("abc123", state)
    assert form(server.requests[0]) == {
        "grant_type": "authorization_code",
        "code": "abc123",
        "redirect_uri": "http://localhost:8000/callback",
    }
    saved = storage.load_tokens()
    assert saved["access_token"] == "user-tok"
    assert saved["refresh_token"] == "refresh-1"
    assert saved["expires_at"] == clock.now + 1800


def test_exchange_code_rejects_replayed_state(client, server):
    server.queue(200, USER_TOKEN)
    state = state_from(client.authorize_url())
    client.exchange_code("abc123", state)
    sent = len(server.requests)
    with pytest.raises(KrogerError):
        client.exchange_code("abc123", state)
    assert len(server.requests) == sent


def test_exchange_code_rejects_wrong_state(client, server, storage):
    client.authorize_url()
    with pytest.raises(KrogerError) as info:
        client.exchange_code("abc123", "not-the-state")
    assert info.value.status == 400
    assert "state" in info.value.message.lower()
    assert server.requests == []
    assert storage.load_tokens() is None


def test_exchange_code_rejects_when_no_sign_in_was_started(client, server):
    with pytest.raises(KrogerError):
        client.exchange_code("abc123", "anything")
    assert server.requests == []


def test_is_signed_in_reflects_stored_tokens(client, storage):
    assert client.is_signed_in() is False
    storage.save_tokens({"access_token": "x", "refresh_token": "y", "expires_at": 0})
    assert client.is_signed_in() is True


def test_user_token_returns_stored_token_when_fresh(client, storage, clock, server):
    storage.save_tokens({"access_token": "fresh", "refresh_token": "r", "expires_at": clock.now + 600})
    assert client.user_token() == "fresh"
    assert server.requests == []


def test_user_token_refreshes_when_expired(client, storage, clock, server):
    storage.save_tokens({"access_token": "old", "refresh_token": "refresh-1", "expires_at": clock.now + 30})
    server.queue(200, {**USER_TOKEN, "access_token": "new", "refresh_token": "refresh-2"})
    assert client.user_token() == "new"
    assert form(server.requests[0]) == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    saved = storage.load_tokens()
    assert saved["access_token"] == "new"
    assert saved["refresh_token"] == "refresh-2"


def test_user_token_raises_not_signed_in_when_no_tokens(client):
    with pytest.raises(NotSignedIn):
        client.user_token()


def test_rejected_refresh_clears_tokens_and_raises_not_signed_in(client, storage, clock, server):
    storage.save_tokens({"access_token": "old", "refresh_token": "dead", "expires_at": clock.now - 1})
    server.queue(401, {"errors": {"reason": "invalid_grant"}})
    with pytest.raises(NotSignedIn) as info:
        client.user_token()
    assert "invalid_grant" in info.value.message
    assert storage.load_tokens() is None


def test_transient_refresh_failure_keeps_tokens(client, storage, clock, server):
    storage.save_tokens({"access_token": "old", "refresh_token": "refresh-1", "expires_at": clock.now - 1})
    server.queue(503, None)
    with pytest.raises(KrogerError) as info:
        client.user_token()
    assert not isinstance(info.value, NotSignedIn)
    assert info.value.status == 503
    assert storage.load_tokens() is not None


def test_add_to_cart_sends_pickup_items(client, storage, clock, server):
    storage.save_tokens({"access_token": "fresh", "refresh_token": "r", "expires_at": clock.now + 600})
    server.queue(204, None)
    client.add_to_cart([("0001111041700", 2), ("0001111041701", 1)])
    req = server.requests[0]
    assert req.method == "PUT"
    assert req.url.path == "/v1/cart/add"
    assert req.headers["Authorization"] == "Bearer fresh"
    assert json.loads(req.content) == {"items": [
        {"upc": "0001111041700", "quantity": 2, "modality": "PICKUP"},
        {"upc": "0001111041701", "quantity": 1, "modality": "PICKUP"},
    ]}


def test_add_to_cart_raises_kroger_error_on_rejection(client, storage, clock, server):
    storage.save_tokens({"access_token": "fresh", "refresh_token": "r", "expires_at": clock.now + 600})
    server.queue(400, {"errors": {"reason": "Invalid UPC 0001111041700"}})
    with pytest.raises(KrogerError) as info:
        client.add_to_cart([("0001111041700", 1)])
    assert info.value.message == "Invalid UPC 0001111041700"


def test_refresh_without_new_refresh_token_keeps_old_one(client, storage, clock, server):
    storage.save_tokens({"access_token": "old", "refresh_token": "refresh-1", "expires_at": clock.now - 1})
    server.queue(200, {"access_token": "new", "expires_in": 1800})
    assert client.user_token() == "new"
    assert storage.load_tokens()["refresh_token"] == "refresh-1"


def test_malformed_tokens_clear_and_raise_not_signed_in(client, storage):
    storage.save_tokens({"access_token": "a", "refresh_token": "r"})
    with pytest.raises(NotSignedIn):
        client.user_token()
    assert storage.load_tokens() is None


def test_add_to_cart_refreshes_expired_token_first(client, storage, clock, server):
    storage.save_tokens({"access_token": "old", "refresh_token": "refresh-1", "expires_at": clock.now - 1})
    server.queue(200, {**USER_TOKEN, "access_token": "new"})
    server.queue(204, None)
    client.add_to_cart([("0001", 1)])
    assert len(server.requests) == 2
    assert server.requests[1].headers["Authorization"] == "Bearer new"
