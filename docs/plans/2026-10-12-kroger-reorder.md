# Kroger Reorder Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A local web page that pushes a saved "standard" grocery list into the user's Kroger cart in one click via the official Kroger API.

**Architecture:** One FastAPI process serves a single vanilla-JS HTML page and a small JSON API. `storage.py` persists three plain JSON files (list, store config, OAuth tokens). `kroger.py` wraps the Kroger API with two auth modes: an app token for product/store search, and a user OAuth token for add-to-cart. Routes in `main.py` glue them; `create_app(storage, kroger)` takes both as parameters so route tests use fakes.

**Tech Stack:** Python 3.14, FastAPI, uvicorn, httpx (with `MockTransport` in tests), python-dotenv, pytest. No database, no frontend build.

**Design doc:** `docs/plans/2026-10-12-kroger-reorder-design.md` — read it first.

**Kroger API reference** (https://developer.kroger.com/documentation/public): if any call shape below fails against the real API, check the docs rather than guessing.

| Purpose | Call |
|---|---|
| App token | `POST /v1/connect/oauth2/token`, Basic auth `client_id:client_secret`, form `grant_type=client_credentials&scope=product.compact` |
| User login | `GET /v1/connect/oauth2/authorize?scope=cart.basic:write&response_type=code&client_id=…&redirect_uri=…` |
| Exchange code | `POST /v1/connect/oauth2/token`, form `grant_type=authorization_code&code=…&redirect_uri=…` |
| Refresh | `POST /v1/connect/oauth2/token`, form `grant_type=refresh_token&refresh_token=…` |
| Stores | `GET /v1/locations?filter.zipCode.near=45202&filter.limit=10` |
| Products | `GET /v1/products?filter.term=milk&filter.locationId=01400943&filter.limit=10` |
| Add to cart | `PUT /v1/cart/add` JSON `{"items":[{"upc":"0001111041700","quantity":1,"modality":"PICKUP"}]}` → 204 |

**Conventions for every task:**
- Run commands from `C:\Projects\kroger-reorder`.
- Use the venv interpreter directly: `.venv/Scripts/python -m pytest …` (no activation needed).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `.env.example`
- Existing: `.gitignore` already has `data/`, `.env`, `.venv/`, caches

**Step 1: Write pyproject.toml**

```toml
[project]
name = "kroger-reorder"
version = "0.1.0"
description = "Fill a Kroger pickup cart from a saved standard grocery list"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "httpx>=0.27",
  "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
kroger-reorder = "app.__main__:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["app"]

[tool.setuptools.package-data]
app = ["static/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**Step 2: Write .env.example**

```
# From your app at https://developer.kroger.com (My Apps -> your app)
KROGER_CLIENT_ID=
KROGER_CLIENT_SECRET=
# Optional. Must match the redirect URL registered on the Kroger app: http://localhost:<PORT>/callback
PORT=8000
```

**Step 3: Create empty package files and the venv, install**

```bash
mkdir -p app/static tests
: > app/__init__.py
: > tests/__init__.py
python -m venv .venv
.venv/Scripts/python -m pip install -q -e ".[dev]"
```

Expected: install completes with no errors. If pydantic fails to build on Python 3.14, run `.venv/Scripts/python -m pip install -q --upgrade pydantic` and retry.

**Step 4: Verify pytest runs (no tests yet)**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `no tests ran`, exit code 5. That is fine.

**Step 5: Commit**

```bash
git add pyproject.toml app/__init__.py tests/__init__.py .env.example
git commit -m "chore: scaffold project

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Storage (list, config, tokens as plain JSON)

**Files:**
- Create: `app/storage.py`
- Test: `tests/test_storage.py`

**Step 1: Write the failing tests**

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_storage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.storage'`

**Step 3: Write the implementation**

```python
# app/storage.py
"""Plain-JSON persistence for the standard list, store config, and Kroger tokens."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Item:
    upc: str
    name: str
    size: str
    quantity: int = 1
    included: bool = True


@dataclass
class ListLoad:
    items: list[Item]
    warning: str | None = None


class Storage:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- generic helpers ----------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _read(self, name: str) -> Any | None:
        """Parsed JSON, or None if the file is missing. Raises ValueError if corrupt."""
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} is not valid JSON: {exc}") from exc

    def _write(self, name: str, value: Any) -> None:
        self._path(name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    # -- standard list -----------------------------------------------------
    def load_list(self) -> ListLoad:
        try:
            raw = self._read("list.json")
        except ValueError as exc:
            return ListLoad(
                items=[],
                warning=f"{exc}. Showing an empty list; the file is untouched until you add an item.",
            )
        if raw is None:
            return ListLoad(items=[])
        if not isinstance(raw, list):
            return ListLoad(items=[], warning="list.json should contain a JSON array. Showing an empty list.")
        return ListLoad(items=[Item(**entry) for entry in raw])

    def save_list(self, items: list[Item]) -> None:
        self._write("list.json", [asdict(i) for i in items])

    # -- store config ------------------------------------------------------
    def load_config(self) -> dict:
        try:
            return self._read("config.json") or {}
        except ValueError:
            return {}

    def save_config(self, config: dict) -> None:
        self._write("config.json", config)

    # -- OAuth tokens ------------------------------------------------------
    def load_tokens(self) -> dict | None:
        try:
            return self._read("tokens.json")
        except ValueError:
            return None

    def save_tokens(self, tokens: dict) -> None:
        self._write("tokens.json", tokens)

    def clear_tokens(self) -> None:
        self._path("tokens.json").unlink(missing_ok=True)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_storage.py -q`
Expected: `6 passed`

**Step 5: Commit**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat: JSON storage for list, store config, and tokens

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Kroger client — app token, store lookup, product search, error parsing

**Files:**
- Create: `app/kroger.py`
- Test: `tests/test_kroger.py`

The client takes an optional `httpx` transport so tests inject `httpx.MockTransport` and a fake clock. Nothing in tests touches the network.

**Step 1: Write the failing tests**

```python
# tests/test_kroger.py
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
```

(The `json`, `urlparse`, and `NotSignedIn` imports are unused until Task 4; that is intentional so Task 4 only appends.)

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.kroger'`

**Step 3: Write the implementation (app-token half)**

```python
# app/kroger.py
"""Thin client for the Kroger public API. Docs: https://developer.kroger.com/documentation/public"""
from __future__ import annotations

import base64
import time
from typing import Callable
from urllib.parse import urlencode

import httpx

from .storage import Storage

BASE_URL = "https://api.kroger.com/v1"
TOKEN_PATH = "/connect/oauth2/token"
AUTHORIZE_URL = f"{BASE_URL}/connect/oauth2/authorize"
APP_SCOPE = "product.compact"
USER_SCOPE = "cart.basic:write"
# Treat a token as expired a minute early so an in-flight call never uses a dead one.
EXPIRY_MARGIN_SECONDS = 60


class KrogerError(Exception):
    """Kroger returned a non-success response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class NotSignedIn(KrogerError):
    """No usable user token; the user must sign in again."""

    def __init__(self, message: str = "Not signed in to Kroger.") -> None:
        super().__init__(401, message)


class KrogerClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        storage: Storage,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.storage = storage
        self.clock = clock
        self.http = httpx.Client(base_url=BASE_URL, transport=transport, timeout=20.0)
        self._app_token: str | None = None
        self._app_token_expires_at: float = 0.0

    # -- low-level helpers -------------------------------------------------
    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        message = response.text or f"HTTP {response.status_code}"
        try:
            body = response.json()
            errors = body.get("errors", body)
            message = errors.get("reason") or errors.get("message") or message
        except (ValueError, AttributeError):
            pass
        raise KrogerError(response.status_code, message)

    def _token_request(self, form: dict) -> dict:
        response = self.http.post(
            TOKEN_PATH,
            data=form,
            headers={"Authorization": self._basic_auth()},
        )
        self._raise_for_error(response)
        return response.json()

    def _get(self, path: str, token: str, params: dict) -> dict:
        response = self.http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        self._raise_for_error(response)
        return response.json()

    # -- app token: product and store lookups, no user sign-in -------------
    def app_token(self) -> str:
        if self._app_token and self.clock() < self._app_token_expires_at - EXPIRY_MARGIN_SECONDS:
            return self._app_token
        body = self._token_request({"grant_type": "client_credentials", "scope": APP_SCOPE})
        self._app_token = body["access_token"]
        self._app_token_expires_at = self.clock() + body.get("expires_in", 1800)
        return self._app_token

    def find_stores(self, zip_code: str) -> list[dict]:
        body = self._get(
            "/locations",
            self.app_token(),
            {"filter.zipCode.near": zip_code, "filter.limit": 10},
        )
        stores = []
        for loc in body.get("data", []):
            addr = loc.get("address") or {}
            parts = [p for p in (addr.get("addressLine1"), addr.get("city")) if p]
            stores.append({"id": loc["locationId"], "name": loc.get("name", ""), "address": ", ".join(parts)})
        return stores

    def search_products(self, term: str, location_id: str) -> list[dict]:
        body = self._get(
            "/products",
            self.app_token(),
            {"filter.term": term, "filter.locationId": location_id, "filter.limit": 10},
        )
        results = []
        for product in body.get("data", []):
            item = (product.get("items") or [{}])[0]
            price = item.get("price") or {}
            results.append({
                "upc": product["upc"],
                "name": product.get("description", ""),
                "brand": product.get("brand", ""),
                "size": item.get("size", ""),
                "price": price.get("promo") or price.get("regular"),
            })
        return results
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: `6 passed`

**Step 5: Commit**

```bash
git add app/kroger.py tests/test_kroger.py
git commit -m "feat: Kroger client for app token, store lookup, product search

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Kroger client — user OAuth, token refresh, add to cart

**Files:**
- Modify: `app/kroger.py` (append methods to `KrogerClient`)
- Test: `tests/test_kroger.py` (append)

**Step 1: Append the failing tests**

```python
# append to tests/test_kroger.py
USER_TOKEN = {"access_token": "user-tok", "refresh_token": "refresh-1", "expires_in": 1800, "token_type": "bearer"}


def test_authorize_url_has_scope_and_redirect(client):
    url = urlparse(client.authorize_url())
    assert url.scheme == "https"
    assert url.netloc == "api.kroger.com"
    assert url.path == "/v1/connect/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q == {
        "scope": "cart.basic:write",
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://localhost:8000/callback",
    }


def test_exchange_code_saves_tokens_with_expiry(client, server, storage, clock):
    server.queue(200, USER_TOKEN)
    client.exchange_code("abc123")
    assert form(server.requests[0]) == {
        "grant_type": "authorization_code",
        "code": "abc123",
        "redirect_uri": "http://localhost:8000/callback",
    }
    saved = storage.load_tokens()
    assert saved["access_token"] == "user-tok"
    assert saved["refresh_token"] == "refresh-1"
    assert saved["expires_at"] == clock.now + 1800


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
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: 9 failures (`AttributeError: 'KrogerClient' object has no attribute 'authorize_url'` and similar), 6 passed.

**Step 3: Append the implementation to `KrogerClient`**

```python
    # -- user token: OAuth authorization-code flow, needed for the cart ----
    def authorize_url(self) -> str:
        query = urlencode({
            "scope": USER_SCOPE,
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        })
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_code(self, code: str) -> None:
        body = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        self._store_user_tokens(body)

    def _store_user_tokens(self, body: dict) -> None:
        self.storage.save_tokens({
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": self.clock() + body.get("expires_in", 1800),
        })

    def is_signed_in(self) -> bool:
        return self.storage.load_tokens() is not None

    def user_token(self) -> str:
        tokens = self.storage.load_tokens()
        if not tokens:
            raise NotSignedIn()
        if self.clock() < tokens["expires_at"] - EXPIRY_MARGIN_SECONDS:
            return tokens["access_token"]
        try:
            body = self._token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        except KrogerError as exc:
            self.storage.clear_tokens()
            raise NotSignedIn(f"Kroger sign-in expired ({exc.message}). Please sign in again.") from exc
        self._store_user_tokens(body)
        return body["access_token"]

    def add_to_cart(self, items: list[tuple[str, int]]) -> None:
        payload = {"items": [{"upc": upc, "quantity": qty, "modality": "PICKUP"} for upc, qty in items]}
        response = self.http.put(
            "/cart/add",
            json=payload,
            headers={"Authorization": f"Bearer {self.user_token()}"},
        )
        self._raise_for_error(response)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_kroger.py -q`
Expected: `15 passed`

**Step 5: Commit**

```bash
git add app/kroger.py tests/test_kroger.py
git commit -m "feat: Kroger user OAuth, silent refresh, add to cart

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Settings loader that fails fast

**Files:**
- Create: `app/settings.py`
- Test: `tests/test_settings.py`

`load_settings` takes an optional env mapping so tests never read the real `.env`.

**Step 1: Write the failing tests**

```python
# tests/test_settings.py
import pytest

from app.settings import load_settings


def test_missing_credentials_exit_with_helpful_message():
    with pytest.raises(SystemExit) as info:
        load_settings(env={})
    msg = str(info.value)
    assert "KROGER_CLIENT_ID" in msg
    assert "KROGER_CLIENT_SECRET" in msg
    assert "developer.kroger.com" in msg


def test_only_missing_names_are_reported():
    with pytest.raises(SystemExit) as info:
        load_settings(env={"KROGER_CLIENT_ID": "cid"})
    assert "KROGER_CLIENT_ID" not in str(info.value)
    assert "KROGER_CLIENT_SECRET" in str(info.value)


def test_defaults_and_redirect_uri():
    s = load_settings(env={"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec"})
    assert s.client_id == "cid"
    assert s.client_secret == "sec"
    assert s.port == 8000
    assert s.redirect_uri == "http://localhost:8000/callback"


def test_custom_port_changes_redirect_uri():
    s = load_settings(env={"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec", "PORT": "9100"})
    assert s.port == 9100
    assert s.redirect_uri == "http://localhost:9100/callback"
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_settings.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.settings'`

**Step 3: Write the implementation**

```python
# app/settings.py
"""Reads Kroger credentials from .env / environment and refuses to start without them."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv

REQUIRED = ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET")


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    port: int

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/callback"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env`, or from .env + os.environ when `env` is None."""
    if env is None:
        load_dotenv()
        env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        sys.exit(
            f"Missing {', '.join(missing)}.\n"
            "Copy .env.example to .env and fill in the values from your app at https://developer.kroger.com."
        )
    return Settings(
        client_id=env["KROGER_CLIENT_ID"],
        client_secret=env["KROGER_CLIENT_SECRET"],
        port=int(env.get("PORT") or 8000),
    )
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_settings.py -q`
Expected: `4 passed`

**Step 5: Commit**

```bash
git add app/settings.py tests/test_settings.py
git commit -m "feat: settings loader that fails fast on missing credentials

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: API routes — state, store, auth, search

**Files:**
- Create: `app/main.py`
- Create: `app/static/index.html` (placeholder, replaced in Task 8)
- Test: `tests/test_routes.py`

Route tests use a hand-written `FakeKroger` that duck-types `KrogerClient`. This keeps route tests about routing and JSON shapes, not HTTP mocking.

**Step 1: Write the failing tests**

```python
# tests/test_routes.py
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
```

Create the placeholder page the last test needs:

```bash
printf '<!doctype html><title>Kroger Reorder</title><p>placeholder' > app/static/index.html
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_routes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

**Step 3: Write the implementation**

```python
# app/main.py
"""FastAPI app: serves the page and a small JSON API over Storage + KrogerClient."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .kroger import KrogerError, NotSignedIn
from .storage import Item, Storage

STATIC_DIR = Path(__file__).resolve().parent / "static"


class StoreChoice(BaseModel):
    id: str
    name: str


class NewItem(BaseModel):
    upc: str
    name: str
    size: str = ""


class ItemModel(BaseModel):
    upc: str
    name: str
    size: str = ""
    quantity: int = 1
    included: bool = True


def create_app(storage: Storage, kroger) -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake)."""
    app = FastAPI(title="Kroger Reorder")

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(_: Request, exc: NotSignedIn):
        return JSONResponse(status_code=401, content={"error": exc.message, "signed_in": False})

    @app.exception_handler(KrogerError)
    async def kroger_error(_: Request, exc: KrogerError):
        return JSONResponse(status_code=502, content={"error": f"Kroger said: {exc.message}"})

    def error(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": message, **extra})

    def items_payload() -> dict:
        loaded = storage.load_list()
        return {"items": [asdict(i) for i in loaded.items], "warning": loaded.warning}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        config = storage.load_config()
        return {"store": config or None, "signed_in": kroger.is_signed_in(), **items_payload()}

    @app.get("/api/stores")
    def stores(zip: str):
        return {"stores": kroger.find_stores(zip)}

    @app.put("/api/store")
    def choose_store(choice: StoreChoice):
        storage.save_config({"store_id": choice.id, "store_name": choice.name})
        return {"store": storage.load_config()}

    @app.get("/auth/login")
    def login():
        return RedirectResponse(kroger.authorize_url())

    @app.get("/callback")
    def callback(code: str):
        kroger.exchange_code(code)
        return RedirectResponse("/")

    @app.get("/api/search")
    def search(term: str):
        config = storage.load_config()
        if not config.get("store_id"):
            return error(400, "Pick a store first.")
        return {"results": kroger.search_products(term, config["store_id"])}

    return app
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_routes.py -q`
Expected: `10 passed`

**Step 5: Commit**

```bash
git add app/main.py app/static/index.html tests/test_routes.py
git commit -m "feat: API routes for state, store, sign-in, and search

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: API routes — list items and push to cart

**Files:**
- Modify: `app/main.py` (add routes before `return app`)
- Test: `tests/test_routes.py` (append)

**Step 1: Append the failing tests**

```python
# append to tests/test_routes.py
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


def test_push_kroger_rejection_flags_bad_upc(client, kroger):
    kroger.cart_error = KrogerError(400, "Invalid UPC 0002")
    body = [{**MILK, "quantity": 1, "included": True}, {**EGGS, "quantity": 1, "included": True}]
    r = client.post("/api/push", json=body)
    assert r.status_code == 502
    assert r.json() == {"error": "Kroger said: Invalid UPC 0002", "bad_upcs": ["0002"]}
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_routes.py -q`
Expected: 7 new failures (`405 Method Not Allowed` or `404`), 10 passed.

**Step 3: Add the routes (insert before `return app` in `create_app`)**

```python
    @app.post("/api/items")
    def add_item(new: NewItem):
        items = storage.load_list().items
        if not any(i.upc == new.upc for i in items):
            items.append(Item(upc=new.upc, name=new.name, size=new.size))
            storage.save_list(items)
        return items_payload()

    @app.put("/api/items")
    def replace_items(models: list[ItemModel]):
        storage.save_list([Item(**m.model_dump()) for m in models])
        return items_payload()

    @app.delete("/api/items/{upc}")
    def remove_item(upc: str):
        storage.save_list([i for i in storage.load_list().items if i.upc != upc])
        return items_payload()

    @app.post("/api/push")
    def push(models: list[ItemModel]):
        items = [Item(**m.model_dump()) for m in models]
        storage.save_list(items)
        chosen = [i for i in items if i.included and i.quantity > 0]
        if not chosen:
            return error(400, "Nothing is checked to send.")
        try:
            kroger.add_to_cart([(i.upc, i.quantity) for i in chosen])
        except NotSignedIn:
            raise
        except KrogerError as exc:
            bad = [i.upc for i in chosen if i.upc in exc.message]
            return error(502, f"Kroger said: {exc.message}", bad_upcs=bad)
        return {"added": [asdict(i) for i in chosen]}
```

**Step 4: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `42 passed` (6 storage + 15 kroger + 4 settings + 17 routes)

**Step 5: Commit**

```bash
git add app/main.py tests/test_routes.py
git commit -m "feat: list item routes and push-to-cart

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The page

**Files:**
- Replace: `app/static/index.html`

No automated test beyond `test_index_serves_html`. Verified by eye in Task 9's smoke test. Keep it vanilla: no framework, no build. Write the file with the Write tool (it is too long for a shell heredoc on Windows).

**Step 1: Write the page**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kroger Reorder</title>
<style>
  :root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --accent:#0b5fff; --danger:#b3261e; --ok:#1b7f3b; --line:#e2e2de; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--ink); }
  main { max-width:760px; margin:0 auto; padding:24px 16px 96px; }
  h1 { font-size:1.5rem; margin:0 0 16px; }
  h2 { font-size:1.05rem; margin:0 0 8px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .muted { color:var(--muted); font-size:.9rem; }
  .banner { padding:10px 12px; border-radius:8px; margin-bottom:16px; display:none; }
  .banner.error { display:block; background:#fde8e6; color:var(--danger); }
  .banner.ok { display:block; background:#e4f5ea; color:var(--ok); }
  .banner.warn { display:block; background:#fff4d6; color:#7a5300; }
  input[type=text], input[type=number] { font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; }
  input[type=number] { width:64px; }
  button { font:inherit; padding:8px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  button.secondary { background:#eceeea; color:var(--ink); }
  button.danger { background:transparent; color:var(--danger); padding:4px 8px; }
  button:disabled { opacity:.5; cursor:default; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  ul { list-style:none; margin:8px 0 0; padding:0; }
  li { display:flex; gap:10px; align-items:center; padding:10px 0; border-top:1px solid var(--line); }
  li:first-child { border-top:0; }
  li .name { flex:1; min-width:0; }
  li .size { color:var(--muted); font-size:.85rem; }
  li.bad { background:#fde8e6; margin:0 -8px; padding:10px 8px; border-radius:8px; }
  .results li { cursor:pointer; }
  .results li:hover { background:#f2f4f0; }
  .price { color:var(--muted); font-size:.9rem; white-space:nowrap; }
  footer { position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:12px 16px; }
  footer .inner { max-width:760px; margin:0 auto; display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
  .hidden { display:none !important; }
</style>
</head>
<body>
<main>
  <h1>Kroger Reorder</h1>
  <div id="banner" class="banner"></div>

  <section id="setup">
    <h2>Setup</h2>
    <div id="store-block">
      <div class="row">
        <input id="zip" type="text" inputmode="numeric" placeholder="Zip code" maxlength="5">
        <button id="find-stores">Find stores</button>
      </div>
      <ul id="store-results" class="results"></ul>
    </div>
    <div id="signin-block" class="row" style="margin-top:12px">
      <span class="muted">Sign in once so the app can add to your cart.</span>
      <a href="/auth/login"><button type="button">Sign in to Kroger</button></a>
    </div>
  </section>

  <section id="add">
    <h2>Add an item</h2>
    <div class="row">
      <input id="term" type="text" placeholder="Search your store, e.g. whole milk" style="flex:1">
      <button id="search">Search</button>
    </div>
    <ul id="search-results" class="results"></ul>
  </section>

  <section>
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Standard list <span id="store-label" class="muted"></span></h2>
      <button id="change-store" class="secondary">Change store</button>
    </div>
    <ul id="items"></ul>
    <p id="empty" class="muted hidden">Nothing saved yet. Search above to add items.</p>
  </section>
</main>

<footer>
  <div class="inner">
    <span class="muted">Start from an empty Kroger cart, or quantities will double.</span>
    <button id="push">Send to cart</button>
  </div>
</footer>

<script>
const $ = (id) => document.getElementById(id);
let state = { store: null, signed_in: false, items: [], warning: null };

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.signed_in === false) { state.signed_in = false; render(); }
    throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { data });
  }
  return data;
}

function banner(kind, text, html = false) {
  const el = $('banner');
  el.className = 'banner' + (kind ? ' ' + kind : '');
  if (html) el.innerHTML = text || ''; else el.textContent = text || '';
}

function cleanItems() {
  return state.items.map(({ upc, name, size, quantity, included }) => ({ upc, name, size, quantity, included }));
}

function render() {
  const setupDone = !!(state.store && state.signed_in);
  $('setup').classList.toggle('hidden', setupDone);
  $('store-block').classList.toggle('hidden', !!state.store);
  $('signin-block').classList.toggle('hidden', !!state.signed_in);
  $('add').classList.toggle('hidden', !state.store);
  $('store-label').textContent = state.store ? `· ${state.store.store_name}` : '';
  $('push').disabled = !setupDone || !state.items.some(i => i.included);

  const ul = $('items');
  ul.innerHTML = '';
  state.items.forEach((item, idx) => {
    const li = document.createElement('li');
    if (item.bad) li.classList.add('bad');
    li.innerHTML = `
      <input type="checkbox" ${item.included ? 'checked' : ''} data-idx="${idx}" data-field="included">
      <div class="name"><div>${esc(item.name)}</div><div class="size">${esc(item.size)}</div></div>
      <input type="number" min="1" value="${item.quantity}" data-idx="${idx}" data-field="quantity">
      <button class="danger" data-remove="${esc(item.upc)}" title="Remove">&#x2715;</button>`;
    ul.appendChild(li);
  });
  $('empty').classList.toggle('hidden', state.items.length > 0);
}

async function load() {
  try {
    state = await api('GET', '/api/state');
    render();
    if (state.warning) banner('warn', state.warning);
  } catch (e) { banner('error', e.message); }
}

async function saveItems() {
  try { const data = await api('PUT', '/api/items', cleanItems()); state.items = data.items; }
  catch (e) { banner('error', e.message); }
}

$('find-stores').onclick = async () => {
  banner();
  try {
    const { stores } = await api('GET', `/api/stores?zip=${encodeURIComponent($('zip').value.trim())}`);
    const ul = $('store-results'); ul.innerHTML = '';
    stores.forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = `<div class="name"><div>${esc(s.name)}</div><div class="size">${esc(s.address)}</div></div>`;
      li.onclick = async () => {
        try {
          const data = await api('PUT', '/api/store', { id: s.id, name: s.name });
          state.store = data.store; ul.innerHTML = ''; render();
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!stores.length) banner('warn', 'No stores found near that zip.');
  } catch (e) { banner('error', e.message); }
};

$('change-store').onclick = () => { state.store = null; render(); };

async function search() {
  banner();
  const term = $('term').value.trim();
  if (!term) return;
  try {
    const { results } = await api('GET', `/api/search?term=${encodeURIComponent(term)}`);
    const ul = $('search-results'); ul.innerHTML = '';
    results.forEach(r => {
      const li = document.createElement('li');
      const price = r.price != null ? `$${Number(r.price).toFixed(2)}` : '';
      li.innerHTML = `<div class="name"><div>${esc(r.name)}</div><div class="size">${esc(r.size)}</div></div><span class="price">${price}</span>`;
      li.onclick = async () => {
        try {
          const data = await api('POST', '/api/items', { upc: r.upc, name: r.name, size: r.size });
          state.items = data.items; ul.innerHTML = ''; $('term').value = ''; render();
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!results.length) banner('warn', `No matches for "${term}" at your store.`);
  } catch (e) { banner('error', e.message); }
}
$('search').onclick = search;
$('term').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });

$('items').addEventListener('change', async (e) => {
  const { idx, field } = e.target.dataset;
  if (idx === undefined) return;
  const item = state.items[idx];
  if (field === 'included') item.included = e.target.checked;
  if (field === 'quantity') item.quantity = Math.max(1, parseInt(e.target.value || '1', 10));
  await saveItems();
  render();
});

$('items').addEventListener('click', async (e) => {
  const upc = e.target.dataset.remove;
  if (!upc) return;
  try {
    const data = await api('DELETE', `/api/items/${encodeURIComponent(upc)}`);
    state.items = data.items; render();
  } catch (err) { banner('error', err.message); }
});

$('push').onclick = async () => {
  banner();
  $('push').disabled = true;
  try {
    const { added } = await api('POST', '/api/push', cleanItems());
    state.items.forEach(i => delete i.bad);
    const n = added.length;
    banner('ok', `Added ${n} item${n === 1 ? '' : 's'} to your Kroger cart. ` +
      `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`, true);
  } catch (e) {
    const bad = (e.data && e.data.bad_upcs) || [];
    state.items.forEach(i => { i.bad = bad.includes(i.upc); });
    banner('error', e.message + (bad.length ? ' The highlighted item was rejected; remove or replace it.' : ''));
  }
  render();
};

load();
</script>
</body>
</html>
```

**Step 2: Run the full suite (index test still passes)**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `42 passed`

**Step 3: Commit**

```bash
git add app/static/index.html
git commit -m "feat: single-page UI for setup, search, list, and push

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Entry point, README, and manual smoke test

**Files:**
- Create: `app/__main__.py`
- Create: `README.md`

**Step 1: Write the entry point**

```python
# app/__main__.py
"""`python -m app` (or `kroger-reorder`): start the local server and open the browser."""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from .kroger import KrogerClient
from .main import create_app
from .settings import load_settings
from .storage import Storage


def main() -> None:
    settings = load_settings()
    storage = Storage()
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    app = create_app(storage, kroger)
    url = f"http://localhost:{settings.port}"
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"Kroger Reorder running at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
```

**Step 2: Verify the fail-fast path without credentials**

Run: `env -u KROGER_CLIENT_ID -u KROGER_CLIENT_SECRET .venv/Scripts/python -m app`
Expected: exits immediately printing `Missing KROGER_CLIENT_ID, KROGER_CLIENT_SECRET.` and the developer.kroger.com hint. (If a real `.env` already exists in the folder, temporarily rename it for this check.)

**Step 3: Write README.md**

````markdown
# Kroger Reorder

A tiny local web page that pushes your saved "standard" grocery list into your Kroger cart in one click. You finish checkout (pickup time, payment) in the Kroger app or at kroger.com.

## One-time setup

### 1. Register a Kroger developer app (free, ~5 minutes)

1. Go to https://developer.kroger.com and sign in with your regular Kroger account.
2. Open **My Apps** -> **Register App**.
3. Name it anything (e.g. `kroger-reorder`).
4. Under **Redirect URL**, enter exactly: `http://localhost:8000/callback`
5. Request the **Products**, **Locations**, and **Cart** APIs.
6. Save, then copy the **Client ID** and **Client Secret**.

### 2. Install

```powershell
cd C:\Projects\kroger-reorder
python -m venv .venv
.venv\Scripts\python -m pip install -e .
copy .env.example .env
```

Open `.env` and paste in your Client ID and Client Secret.

### 3. Run

```powershell
.venv\Scripts\python -m app
```

Your browser opens to http://localhost:8000. Enter your zip, pick your store, and click **Sign in to Kroger**. You only do this once; the app stays signed in using a refresh token.

## Everyday use

1. Empty your Kroger cart in the app (the API can only *add*; it can't see or clear what's already there).
2. Run `.venv\Scripts\python -m app`.
3. Uncheck anything you don't need this trip, tweak quantities, click **Send to cart**.
4. Open the Kroger app, choose a pickup time, pay.

To add an item to the standard list, search for it in the **Add an item** box and click the match.

## Where your data lives

Everything is plain JSON in `data/` (gitignored):

- `list.json` - your standard list. Safe to edit by hand.
- `config.json` - chosen store.
- `tokens.json` - Kroger sign-in tokens. Delete this file to sign out.

## Troubleshooting

- **"Missing KROGER_CLIENT_ID..."** - `.env` isn't filled in.
- **Sign-in button came back** - Kroger rejected the refresh token. Just sign in again; your list is untouched.
- **An item is highlighted red after Send** - Kroger rejected that UPC (often discontinued). Remove it and search for a replacement.
- **"Kroger said: ..."** - the raw message from Kroger's API. If it mentions rate limits, wait a minute.

## Development

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

Tests never hit the network; the Kroger client is exercised through `httpx.MockTransport`.

### Manual smoke test (do this once after setup)

1. Start the app, finish store + sign-in.
2. Search `milk`, click one result.
3. Click **Send to cart**.
4. Open the Kroger app: the item should be in your cart with pickup selected.
````

**Step 4: Run the full suite one more time**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `42 passed`

**Step 5: Commit and push**

```bash
git add app/__main__.py README.md
git commit -m "feat: entry point and README with Kroger app setup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

**Step 6: Hand off to the user for the real smoke test**

The user must register the Kroger developer app and fill in `.env` themselves. Then they run the manual smoke test in the README. Report what to do in plain terms; do not claim the end-to-end flow works until they confirm.

---

## Done criteria

- `.venv/Scripts/python -m pytest -q` reports `42 passed`.
- `python -m app` with no `.env` exits with the credential hint.
- With real credentials: store picked, signed in, one item searched and pushed, item visible in the Kroger app cart.
