# kroger_reorder/stores.py
"""The stores a list can belong to. Kroger has a catalog and a cart API. Aldi has a catalog (read from
aldi.us, see aldi.py) but no cart. Meijer and Walmart have neither. Every store without a cart is a
hand-off store: the list is shopped on the store's own site through "Open at <store>", and plain items
(names with no product) are allowed on it. See docs/plans/2026-11-02-multi-store-design.md and
docs/plans/2026-11-05-aldi-catalog-design.md."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .storage import DEFAULT_STORE, slugify


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    catalog: bool                   # product search, matching and prices (needs the store's client)
    cart: bool                      # Send to cart; only these lists require catalog products
    search_url: str | None = None   # "{term}" is replaced by the URL-encoded search term
    home_url: str | None = None
    product_url: str | None = None  # "{id}" is replaced by a catalog product id, for Open at <store>
    product_prefix: str = ""        # catalog ids are stored as prefix + digits; Kroger UPCs are bare digits


PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    Provider("kroger", "Kroger", catalog=True, cart=True),
    Provider("aldi", "Aldi", catalog=True, cart=False, search_url="https://www.aldi.us/store/aldi/s?k={term}",
             home_url="https://www.aldi.us/", product_url="https://www.aldi.us/store/aldi/products/{id}",
             product_prefix="aldi:"),
    Provider("meijer", "Meijer", catalog=False, cart=False,
             search_url="https://www.meijer.com/shopping/search.html?text={term}", home_url="https://www.meijer.com/"),
    Provider("walmart", "Walmart", catalog=False, cart=False,
             search_url="https://www.walmart.com/search?q={term}", home_url="https://www.walmart.com/"),
)}
assert DEFAULT_STORE in PROVIDERS and PROVIDERS[DEFAULT_STORE].cart

__all__ = ["DEFAULT_STORE", "PROVIDERS", "Provider", "UnknownStore", "item_key", "provider", "search_link",
           "is_product", "shop_link", "store_names"]


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


def is_product(key: str, upc: str) -> bool:
    """Is `upc` a product from this store's catalog (rather than a plain item's name key, or another
    store's id)? Kroger: all digits. Aldi: "aldi:" + digits. Name keys never contain ":"."""
    p = provider(key)
    if not p.catalog:
        return False
    rest = upc[len(p.product_prefix):] if upc.startswith(p.product_prefix) else ""
    return rest.isdigit()


def shop_link(key: str, upc: str, name: str) -> str:
    """Where Open at <store> sends an item: its product page when it is a catalog product, else a search."""
    p = provider(key)
    if p.product_url and is_product(key, upc):
        return p.product_url.format(id=upc[len(p.product_prefix):])
    return search_link(key, name)


def item_key(name: str) -> str:
    """Plain items have no UPC; the slug of the name is their identity, so "Bananas" twice is one row.
    Names that differ only in accents, case or punctuation share a key; that is the intended merge."""
    return slugify(name, fallback="item")
