"""Turn a recipe web page into grocery suggestions.

1. `fetch_page()` downloads the page (browser-like User-Agent, redirects, 2 MB cap, HTML only).
2. `extract()` pulls the ingredient lines out of the page's schema.org JSON-LD `Recipe` object, which
   nearly every recipe site embeds; when there is none, the page's visible text is kept instead.
3. `suggest()` asks Claude (through `vision.complete`, so the same API or CLI backend as photos) to
   turn the lines into store purchases, marking what the list already has and what is a pantry staple.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .vision import VisionError

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 15.0
HTML_TYPES = ("text/html", "application/xhtml+xml")
HEADERS = {
    # Many recipe sites answer 403 to anything that doesn't look like a browser.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.8",
}

BAD_URL = "Enter a full web address starting with http:// or https://."
NOT_HTML = "That address isn't a web page. Paste the link to the recipe itself."
TOO_LARGE = "That page is too large to read."
NO_RECIPE = ("Couldn't find a recipe on that page. Try the recipe's own page rather than a search or "
             "category page.")


class RecipeError(VisionError):
    """Same contract as VisionError (status + message); a distinct class so tests can tell them apart.
    `main.py` answers both with the same handler."""


@dataclass
class Page:
    url: str   # final URL after redirects
    html: str


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RecipeError(400, BAD_URL)
    return url


def fetch_page(url: str, *, transport: httpx.BaseTransport | None = None) -> Page:
    url = validate_url(url)
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise RecipeError(502, f"That site refused the request (HTTP {response.status_code}). "
                                           "Copy the ingredients from the page and use Import instead.")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type not in HTML_TYPES:
                    raise RecipeError(415, NOT_HTML)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise RecipeError(413, TOO_LARGE)
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                final_url = str(response.url)
    except httpx.HTTPError as exc:
        reason = str(exc) or exc.__class__.__name__
        raise RecipeError(502, f"Couldn't fetch that page: {reason}. Check the address and try again.") from exc
    return Page(url=final_url, html=b"".join(chunks).decode(encoding, errors="replace"))
