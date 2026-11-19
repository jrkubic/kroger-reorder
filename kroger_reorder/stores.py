# kroger_reorder/stores.py
"""The stores a list can belong to. Kroger has a catalog and a cart API. Aldi has a catalog (read from
aldi.us, see aldi.py) and, since v7.2, a linked cart: the user's own aldi.us cart, reached through a
session cookie they paste in. Meijer has a catalog since v7.4, read through its search service (see
meijer.py), with typical rather than per-store prices, and no cart. Walmart has neither. Every store
without a cart is a hand-off store: the list is shopped on the store's own site through "Open at
<store>", and plain items (names with no product) are allowed on it. See
docs/plans/2026-11-02-multi-store-design.md, docs/plans/2026-11-05-aldi-catalog-design.md,
docs/plans/2026-11-06-aldi-cart-design.md and docs/plans/2026-11-18-meijer-catalog-design.md."""
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
    product_url: str | None = None  # "{id}" is replaced by a catalog product id and "{slug}" by the item's
                                    # name as a URL slug, for Open at <store>
    product_prefix: str = ""        # catalog ids are stored as prefix + digits; Kroger UPCs are bare digits
    linked_cart: bool = False       # a cart reached through the user's pasted sign-in (Aldi); the list stays
                                    # a hand-off list: plain items allowed, Open at <store> still offered
    approx_prices: bool = False     # prices are a typical figure across the chain's stores, not the user's store


PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    Provider("kroger", "Kroger", catalog=True, cart=True),
    Provider("aldi", "Aldi", catalog=True, cart=False, search_url="https://www.aldi.us/store/aldi/s?k={term}",
             home_url="https://www.aldi.us/", product_url="https://www.aldi.us/store/aldi/products/{id}",
             product_prefix="aldi:", linked_cart=True),
    Provider("meijer", "Meijer", catalog=True, cart=False,
             search_url="https://www.meijer.com/shopping/search.html?text={term}", home_url="https://www.meijer.com/",
             # meijer.com finds the page by the id; the slug only reads well (any text works, checked 2026-11-19).
             product_url="https://www.meijer.com/shopping/product/{slug}/{id}.html",
             product_prefix="meijer:", approx_prices=True),
    Provider("walmart", "Walmart", catalog=False, cart=False,
             search_url="https://www.walmart.com/search?q={term}", home_url="https://www.walmart.com/"),
)}
assert DEFAULT_STORE in PROVIDERS and PROVIDERS[DEFAULT_STORE].cart

__all__ = ["DEFAULT_STORE", "PROVIDERS", "CatalogError", "Provider", "UnknownStore", "item_key", "provider",
           "search_link", "is_product", "has_product_page", "shop_link", "store_names"]


def store_names() -> str:
    """'Kroger, Aldi, Meijer, or Walmart', for error messages."""
    names = [p.name for p in PROVIDERS.values()]
    return ", ".join(names[:-1]) + f", or {names[-1]}"


class CatalogError(Exception):
    """A catalog store's site or service failed. `status` 0 means it couldn't be reached."""
    changed_message = ""   # each store's "site changed" message; that and status 0 mean the whole catalog is down

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message

    @property
    def unavailable(self) -> bool:
        """The whole catalog is out of reach (no network, or the site changed), not just one search."""
        return self.status == 0 or (bool(self.changed_message) and self.message == self.changed_message)


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
    store's id)? Kroger: all digits. Aldi: "aldi:" + digits. Meijer: "meijer:" + digits. Name keys never
    contain ":"."""
    p = provider(key)
    if not p.catalog:
        return False
    rest = upc[len(p.product_prefix):] if upc.startswith(p.product_prefix) else ""
    return rest.isdigit()


def has_product_page(key: str, upc: str) -> bool:
    """Does Open at <store> link this item to its own product page (rather than a search)?"""
    return bool(provider(key).product_url) and is_product(key, upc)


def shop_link(key: str, upc: str, name: str) -> str:
    """Where Open at <store> sends an item: its product page when it is a catalog product, else a search."""
    p = provider(key)
    if has_product_page(key, upc):
        return p.product_url.format(id=upc[len(p.product_prefix):], slug=slugify(name, fallback="item"))
    return search_link(key, name)


def item_key(name: str) -> str:
    """Plain items have no UPC; the slug of the name is their identity, so "Bananas" twice is one row.
    Names that differ only in accents, case or punctuation share a key; that is the intended merge."""
    return slugify(name, fallback="item")
