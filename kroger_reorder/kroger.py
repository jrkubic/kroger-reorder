"""Thin client for the Kroger public API. Docs: https://developer.kroger.com/documentation/public"""
from __future__ import annotations

import base64
import secrets
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
# Product image sizes, best first; the page shows a small thumbnail so medium is plenty.
IMAGE_SIZE_PREFERENCE = ("medium", "large", "small", "xlarge", "thumbnail")


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
        self._pending_state: str | None = None  # CSRF guard for the OAuth redirect

    # -- low-level helpers -------------------------------------------------
    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        text = response.text.strip()
        if not text or text.startswith("<"):  # empty or HTML error page: not worth showing
            message = f"HTTP {response.status_code}"
        else:
            message = text[:200]
        try:
            body = response.json()
            errors = body.get("errors", body)
            message = str(
                errors.get("reason") or errors.get("message") or errors.get("error_description") or message
            )
        except (ValueError, AttributeError):
            pass
        raise KrogerError(response.status_code, message)

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One place where transport failures (DNS, refused, timeout) become KrogerError."""
        try:
            return self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KrogerError(0, f"Could not reach Kroger: {exc.__class__.__name__}") from exc

    def _token_request(self, form: dict) -> dict:
        response = self._send(
            "POST",
            TOKEN_PATH,
            data=form,
            headers={"Authorization": self._basic_auth()},
        )
        self._raise_for_error(response)
        return response.json()

    def _get(self, path: str, token: str, params: dict) -> dict:
        response = self._send("GET", path, params=params, headers={"Authorization": f"Bearer {token}"})
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

    @staticmethod
    def _image_url(product: dict) -> str | None:
        """Front image first; within an image, the best size per IMAGE_SIZE_PREFERENCE, else any url."""
        images = product.get("images") or []
        ordered = sorted(images, key=lambda im: 0 if im.get("perspective") == "front" or im.get("featured") else 1)
        for image in ordered:
            by_size = {s.get("size"): s.get("url") for s in (image.get("sizes") or []) if s.get("url")}
            if not by_size:
                continue
            for preferred in IMAGE_SIZE_PREFERENCE:
                if preferred in by_size:
                    return by_size[preferred]
            return next(iter(by_size.values()))
        return None

    def _summarize(self, product: dict) -> dict:
        item = (product.get("items") or [{}])[0]
        price = item.get("price") or {}
        return {
            "upc": product["upc"],
            "name": product.get("description", ""),
            "brand": product.get("brand", ""),
            "size": item.get("size", ""),
            "price": price.get("regular") or None,
            "promo": price.get("promo") or None,
            "image": self._image_url(product),
        }

    def search_products(self, term: str, location_id: str) -> list[dict]:
        body = self._get(
            "/products",
            self.app_token(),
            {"filter.term": term, "filter.locationId": location_id, "filter.limit": 10},
        )
        return [self._summarize(p) for p in body.get("data", [])]

    def lookup_product(self, upc: str, location_id: str) -> dict:
        body = self._get(f"/products/{upc}", self.app_token(), {"filter.locationId": location_id})
        data = body.get("data")
        if not isinstance(data, dict) or "upc" not in data:
            raise KrogerError(502, f"Kroger returned no product data for {upc}")
        return self._summarize(data)

    # -- user token: OAuth authorization-code flow, needed for the cart ----
    def authorize_url(self) -> str:
        self._pending_state = secrets.token_urlsafe(16)
        query = urlencode({
            "scope": USER_SCOPE,
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": self._pending_state,
        })
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_code(self, code: str, state: str | None) -> None:
        if self._pending_state is None or state != self._pending_state:
            raise KrogerError(400, "Sign-in state mismatch; please try signing in again.")
        self._pending_state = None
        body = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        self._store_user_tokens(body)

    def _store_user_tokens(self, body: dict, fallback_refresh: str | None = None) -> None:
        self.storage.save_tokens({
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or fallback_refresh,
            "expires_at": self.clock() + body.get("expires_in", 1800),
        })

    def is_signed_in(self) -> bool:
        return self.storage.load_tokens() is not None

    def user_token(self) -> str:
        tokens = self.storage.load_tokens()
        if not tokens:
            raise NotSignedIn()
        try:
            expires_at = float(tokens["expires_at"])
            access, refresh = tokens["access_token"], tokens["refresh_token"]
        except (KeyError, TypeError, ValueError):
            self.storage.clear_tokens()
            raise NotSignedIn("Stored Kroger sign-in is unreadable. Please sign in again.")
        if self.clock() < expires_at - EXPIRY_MARGIN_SECONDS:
            return access
        try:
            body = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        except KrogerError as exc:
            if exc.status not in (400, 401):
                raise  # transient (5xx, network): keep the tokens and let the caller retry later
            self.storage.clear_tokens()
            raise NotSignedIn(f"Kroger sign-in expired ({exc.message}). Please sign in again.") from exc
        self._store_user_tokens(body, fallback_refresh=refresh)
        return body["access_token"]

    def add_to_cart(self, items: list[tuple[str, int]]) -> None:
        payload = {"items": [{"upc": upc, "quantity": qty, "modality": "PICKUP"} for upc, qty in items]}
        response = self._send(
            "PUT",
            "/cart/add",
            json=payload,
            headers={"Authorization": f"Bearer {self.user_token()}"},
        )
        self._raise_for_error(response)
