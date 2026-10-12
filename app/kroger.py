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
