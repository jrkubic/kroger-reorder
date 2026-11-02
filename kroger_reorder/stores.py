# kroger_reorder/stores.py
"""The stores a list can belong to. Kroger has a catalog and a cart API; the others are hand-off
stores: the app keeps plain items and opens the store's own site to shop them, because none of them
offers a public API an individual can fill a cart with (see docs/plans/2026-11-02-multi-store-design.md)."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .storage import DEFAULT_STORE, slugify


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    has_catalog: bool               # True: UPCs, catalog search, prices, Send to cart (Kroger only)
    search_url: str | None = None   # "{term}" is replaced by the URL-encoded search term
    home_url: str | None = None


PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    Provider("kroger", "Kroger", True),
    Provider("aldi", "Aldi", False, "https://www.aldi.us/store/aldi/s?k={term}", "https://www.aldi.us/"),
    Provider("meijer", "Meijer", False, "https://www.meijer.com/shopping/search.html?text={term}", "https://www.meijer.com/"),
    Provider("walmart", "Walmart", False, "https://www.walmart.com/search?q={term}", "https://www.walmart.com/"),
)}
assert DEFAULT_STORE in PROVIDERS and PROVIDERS[DEFAULT_STORE].has_catalog

__all__ = ["DEFAULT_STORE", "PROVIDERS", "Provider", "UnknownStore", "item_key", "provider", "search_link",
           "store_names"]


def store_names() -> str:
    """'Kroger, Aldi, Meijer, or Walmart', for error messages."""
    names = [p.name for p in PROVIDERS.values()]
    return ", ".join(names[:-1]) + f", or {names[-1]}"


class UnknownStore(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key
        self.message = f"Unknown store '{key}'. Choose {store_names()}."


def provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise UnknownStore(key) from None


def search_link(key: str, term: str) -> str:
    p = provider(key)
    if not p.search_url:
        raise ValueError(f"{p.name} has no search link; its lists go to the cart")
    return p.search_url.format(term=quote(term, safe=""))


def item_key(name: str) -> str:
    """Plain items have no UPC; the slug of the name is their identity, so "Bananas" twice is one row.
    Names that differ only in accents, case or punctuation share a key; that is the intended merge."""
    return slugify(name, fallback="item")
