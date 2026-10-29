# Recipe URL suggestions (v6) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development)
> to implement this plan task-by-task. Design: `docs/plans/2026-10-29-recipe-url-design.md`.

**Goal:** Paste a recipe's web address; the app proposes the groceries it needs, flags what the chosen
list already has and what is a pantry staple (both preset to Skip), and opens the existing import review.

**Architecture:** A new `kroger_reorder/recipe.py` fetches the page with httpx, pulls the ingredient
lines from schema.org JSON-LD (page text as fallback), and asks Claude to turn them into grocery items
with the list and purchase history in hand. The API-or-CLI dispatch and error mapping already in
`vision.py` are extracted into one generic `complete()` that both photos and recipes use. A new route
`POST /api/recipe/suggest` returns lines in the same shape as the photo route, so the page reuses the
import review table.

**Tech Stack:** FastAPI, httpx (`MockTransport` in tests), `html.parser` from the standard library,
`anthropic` SDK or `claude -p`, vanilla JS. No new dependencies, no new settings.

**Conventions (from AGENTS.md):** TDD per task, Conventional Commits ending with
`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`, run the whole suite with
`.venv/Scripts/python -m pytest -q` (Windows) before every commit. Tests never touch the network or
`data/`. Work on branch `recipe-url`. Never print `.env`.

---

### Task 1: Generic `complete()` in vision.py (photos unchanged)

Pull the backend dispatch and SDK error mapping out of `analyze_api`/`analyze_cli` so the recipe
module can reuse them. Every existing photo test must keep passing; the quoted README strings keep
their exact text.

**Files:**
- Modify: `kroger_reorder/vision.py`
- Test: `tests/test_vision.py`

**Step 1: Write the failing tests** (append to `tests/test_vision.py`; add `Wording, complete` to the
import line at the top)

```python
# -- generic complete() -----------------------------------------------------------------

WORDING = Wording(timeout="try a shorter page", refusal="Claude declined; try another page.",
                  truncated="Claude's answer was cut off; try a shorter page.")


def test_complete_cli_without_tools_passes_no_allowed_tools_and_one_turn():
    run = fake_runner(stdout=envelope("[]"))
    text = complete("SYS", "USER", wording=WORDING, model="m", timeout=5, runner=run, which=which_ok)
    assert text == "[]"
    (args, stdin_text, timeout), = run.calls
    assert "--allowedTools" not in args
    assert args[args.index("--max-turns") + 1] == "1"
    assert stdin_text.startswith("USER") and stdin_text.rstrip().endswith("SYS")


def test_complete_cli_with_tools_and_preamble():
    run = fake_runner(stdout=envelope("[]"))
    complete("SYS", "USER", cli_preamble="Read these files", cli_tools=("Read",), wording=WORDING,
             model="m", timeout=5, runner=run, which=which_ok)
    (args, stdin_text, _), = run.calls
    assert args[args.index("--allowedTools") + 1] == "Read"
    assert args[args.index("--max-turns") + 1] == "6"
    assert stdin_text.startswith("Read these files")


def test_complete_cli_timeout_uses_callers_wording():
    def slow(args, stdin_text, timeout):
        raise CliTimeout()
    with pytest.raises(VisionError) as exc:
        complete("SYS", "USER", wording=WORDING, model="m", timeout=9, runner=slow, which=which_ok)
    assert exc.value.status == 504 and exc.value.message == "Claude took longer than 9 seconds; try a shorter page."


def test_complete_api_without_images_sends_only_text():
    make = fake_client(result=api_message("[]"))
    text = complete("SYS", "USER", wording=WORDING, model="m", timeout=5, api_key="k", make_client=make)
    assert text == "[]"
    (call,) = make.calls
    assert call["system"] == "SYS"
    assert call["messages"] == [{"role": "user", "content": [{"type": "text", "text": "USER"}]}]


def test_complete_api_refusal_and_truncation_use_callers_wording():
    for stop, expected in (("refusal", "Claude declined; try another page."),
                           ("max_tokens", "Claude's answer was cut off; try a shorter page.")):
        make = fake_client(result=api_message("", stop_reason=stop))
        with pytest.raises(VisionError) as exc:
            complete("SYS", "USER", wording=WORDING, model="m", timeout=5, api_key="k", make_client=make)
        assert exc.value.status == 502 and exc.value.message == expected


def test_parse_suggestions_accepts_custom_kinds():
    out = parse_suggestions(json.dumps([{"name": "flour", "kind": "staple"}, {"name": "milk", "kind": "seen"}]),
                            kinds=("needed", "staple", "on_list"))
    assert [(s.name, s.kind) for s in out] == [("flour", "staple"), ("milk", "needed")]
```

Also add `CliTimeout` to the import line.

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_vision.py -q`
Expected: ImportError (`Wording`, `complete`, `CliTimeout` missing).

**Step 3: Implement** in `kroger_reorder/vision.py`

Replace the section from `def _timeout_error` through the end of the file with the code below, keeping
`MAX_ITEMS`, `MAX_QUANTITY`, `KINDS`, `API_MAX_TOKENS`, `API_MAX_RETRIES`, `MEDIA_TYPES`, `SYSTEM`,
`NO_BACKEND`, `VisionError`, `Suggestion`, `RunResult`, `Runner`, `Which`, `choose_backend`,
`context_text`, `_extract_json_array`, and `_image_block` exactly as they are. Add
`from collections.abc import Callable, Sequence` at the top.

```python
@dataclass(frozen=True)
class Wording:
    """Feature-specific halves of the messages the shared backend code raises."""
    timeout: str    # "...; <timeout>." after "Claude took longer than N seconds"
    refusal: str    # whole message when Claude refuses
    truncated: str  # whole message when the answer hit max_tokens


PHOTO_WORDING = Wording(
    timeout="try fewer or smaller photos",
    refusal="Claude declined to analyze these photos; try different ones.",
    truncated="Claude's answer was cut off; try fewer photos or a shorter note.",
)


class CliTimeout(Exception):
    """Raised by a runner when `claude` exceeds the timeout; `complete()` turns it into a 504."""


def _timeout_error(timeout: float, hint: str) -> VisionError:
    return VisionError(504, f"Claude took longer than {int(timeout)} seconds; {hint}.")
```

`parse_suggestions` gains a `kinds` parameter; the default kind is the first entry:

```python
def parse_suggestions(result_text: str, kinds: Sequence[str] = KINDS) -> list[Suggestion]:
    ...
        kind = str(entry.get("kind") or kinds[0]).lower()
        out.append(Suggestion(
            name=name,
            quantity=min(MAX_QUANTITY, max(1, quantity)),
            kind=kind if kind in kinds else kinds[0],
            reason=str(entry.get("reason") or "").strip(),
        ))
```

Backends:

```python
# -- API backend ------------------------------------------------------------------------

def complete_api(system: str, user_text: str, *, images: Sequence[Path] = (), wording: Wording,
                 model: str, timeout: float, api_key: str,
                 make_client: Callable[..., object] = anthropic.Anthropic) -> str:
    client = make_client(api_key=api_key, timeout=timeout, max_retries=API_MAX_RETRIES)
    content = [_image_block(p) for p in images] + [{"type": "text", "text": user_text}]
    try:
        message = client.messages.create(
            model=model,
            max_tokens=API_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise VisionError(502, "Anthropic rejected ANTHROPIC_API_KEY; check the key in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise VisionError(503, "Anthropic rate limit reached; wait a minute and try again.") from exc
    except anthropic.NotFoundError as exc:
        raise VisionError(502, f"Anthropic doesn't know model '{model}'; check PHOTO_MODEL in .env.") from exc
    except (anthropic.OverloadedError, anthropic.ServiceUnavailableError) as exc:
        raise VisionError(503, "Anthropic is busy; try again in a minute.") from exc
    except anthropic.APITimeoutError as exc:
        raise _timeout_error(timeout, wording.timeout) from exc
    except anthropic.APIConnectionError as exc:
        raise VisionError(502, "Couldn't reach Anthropic; check the network on the computer running the app.") from exc
    except anthropic.APIStatusError as exc:
        detail = exc.message
        if isinstance(exc.body, dict):
            error = exc.body.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or detail
        raise VisionError(502, f"Anthropic error: {detail}") from exc
    except anthropic.APIError as exc:
        raise VisionError(502, f"Anthropic error: {exc.message}") from exc
    if message.stop_reason == "refusal":
        raise VisionError(502, wording.refusal)
    if message.stop_reason == "max_tokens":
        raise VisionError(502, wording.truncated)
    return "".join(block.text for block in message.content if block.type == "text")


# -- CLI backend ------------------------------------------------------------------------

def run_cli(args: list[str], stdin_text: str, timeout: float) -> RunResult:
    """Run the CLI with the prompt on stdin. On Windows `claude` is a .CMD shim, and cmd.exe cuts a
    multi-line argument at the first newline, so the prompt must never travel as an argument."""
    try:
        proc = subprocess.run(args, input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CliTimeout() from exc
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def cli_prompt(system: str, user_text: str, cli_preamble: str = "") -> str:
    parts = [cli_preamble, user_text, system] if cli_preamble else [user_text, system]
    return "\n\n".join(parts)


def complete_cli(system: str, user_text: str, *, cli_preamble: str = "", cli_tools: Sequence[str] = (),
                 wording: Wording, model: str, timeout: float,
                 runner: Runner = run_cli, which: Which = shutil.which) -> str:
    exe = which("claude")
    if not exe:
        raise VisionError(503, NO_BACKEND)
    args = [exe, "-p", "--output-format", "json"]
    if cli_tools:
        args += ["--allowedTools", ",".join(cli_tools), "--max-turns", "6"]
    else:
        args += ["--max-turns", "1"]  # a plain answer; no tools, so one turn is all it may take
    if model:
        args += ["--model", model]
    try:
        result = runner(args, cli_prompt(system, user_text, cli_preamble), timeout)
    except CliTimeout as exc:
        raise _timeout_error(timeout, wording.timeout) from exc
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or result.stdout.strip().splitlines() or ["no details"])[-1]
        raise VisionError(502, f"Claude Code failed: {tail}")
    try:
        envelope = json.loads(result.stdout)
    except ValueError as exc:
        raise VisionError(502, "Claude Code returned something that wasn't JSON; try again.") from exc
    if not isinstance(envelope, dict):
        raise VisionError(502, "Claude Code returned an unexpected shape; try again.")
    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        raise VisionError(502, f"Claude Code reported an error: {envelope.get('result') or envelope.get('subtype')}")
    return str(envelope.get("result") or "")


# -- shared entry point -----------------------------------------------------------------

def complete(system: str, user_text: str, *, images: Sequence[Path] = (), cli_preamble: str = "",
             cli_tools: Sequence[str] = (), wording: Wording, model: str, timeout: float,
             api_key: str | None = None, make_client: Callable[..., object] = anthropic.Anthropic,
             runner: Runner = run_cli, which: Which = shutil.which) -> str:
    """Ask Claude for a text answer. API backend when `api_key` is given, otherwise the Claude Code CLI.
    `images` go inline on the API; on the CLI the caller names them in `cli_preamble` and grants the
    `Read` tool through `cli_tools`."""
    if api_key:
        return complete_api(system, user_text, images=images, wording=wording, model=model, timeout=timeout,
                            api_key=api_key, make_client=make_client)
    return complete_cli(system, user_text, cli_preamble=cli_preamble, cli_tools=cli_tools, wording=wording,
                        model=model, timeout=timeout, runner=runner, which=which)


# -- photos -----------------------------------------------------------------------------

def photo_preamble(photos: list[Path]) -> str:
    return "\n".join(["Use the Read tool to look at each of these photos:"] + [f"- {p}" for p in photos])


def build_prompt(photos: list[Path], note: str, profile: str) -> str:
    """The exact text the CLI backend receives for photos (kept for tests and debugging)."""
    return cli_prompt(SYSTEM, context_text(note, profile), photo_preamble(photos))


def analyze(photos: list[Path], note: str, profile: str, *, model: str, timeout: float,
            api_key: str | None = None, make_client: Callable[..., object] = anthropic.Anthropic,
            runner: Runner = run_cli, which: Which = shutil.which) -> list[Suggestion]:
    """Photo suggestions: API backend when `api_key` is given, otherwise the Claude Code CLI."""
    text = complete(SYSTEM, context_text(note, profile), images=photos, cli_preamble=photo_preamble(photos),
                    cli_tools=("Read",), wording=PHOTO_WORDING, model=model, timeout=timeout, api_key=api_key,
                    make_client=make_client, runner=runner, which=which)
    return parse_suggestions(text)
```

Delete the old `analyze_api` and `analyze_cli`. Check `tests/test_vision.py` for direct uses of them
(`grep -n "analyze_api\|analyze_cli" tests/`) and port those tests to `analyze(..., api_key=...)` or
`analyze(..., runner=...)`; the assertions themselves must not change. The CLI photo prompt is now
joined with blank lines; `test_prompt_mentions_photos_note_and_history` only checks containment, so
it still passes. Update the module docstring's first line to say the module also hosts the shared
Claude call used by `recipe.py`.

**Step 4: Run the suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass (213 + 6 new).

**Step 5: Commit**

```bash
git add kroger_reorder/vision.py tests/test_vision.py
git commit -m "refactor(vision): shared complete() for API/CLI backends; wording and kinds per caller"
```

---

### Task 2: `recipe.fetch_page()`

**Files:**
- Create: `kroger_reorder/recipe.py`
- Create: `tests/test_recipe.py`

**Step 1: Write the failing tests**

```python
# tests/test_recipe.py
import json

import httpx
import pytest

from kroger_reorder.recipe import (BAD_URL, NO_RECIPE, NOT_HTML, TOO_LARGE, MAX_BYTES, RecipeError,
                                   fetch_page)


def transport(handler):
    return httpx.MockTransport(handler)


def html_response(body="<html><body>hi</body></html>", status=200, content_type="text/html; charset=utf-8"):
    return httpx.Response(status, content=body.encode("utf-8"), headers={"content-type": content_type})


# -- fetch_page -----------------------------------------------------------------------

@pytest.mark.parametrize("url", ["", "   ", "allrecipes.com/recipe/1", "ftp://x/y", "http://", "javascript:alert(1)"])
def test_fetch_rejects_urls_without_http_scheme_and_host(url):
    with pytest.raises(RecipeError) as exc:
        fetch_page(url, transport=transport(lambda r: html_response()))
    assert exc.value.status == 400 and exc.value.message == BAD_URL


def test_fetch_returns_html_and_final_url_after_redirect():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("user-agent", "")))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://site.test/new"})
        return html_response("<html><title>Soup</title></html>")
    page = fetch_page("https://site.test/old", transport=transport(handler))
    assert page.url == "https://site.test/new" and "<title>Soup</title>" in page.html
    assert all("Mozilla" in ua for _, ua in seen), "recipe sites block non-browser user agents"


def test_fetch_http_error_is_502_with_status_and_import_hint():
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r", transport=transport(lambda r: html_response(status=403)))
    assert exc.value.status == 502
    assert exc.value.message == ("That site refused the request (HTTP 403). Copy the ingredients from the page "
                                 "and use Import instead.")


def test_fetch_non_html_is_415():
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r.pdf", transport=transport(lambda r: html_response(content_type="application/pdf")))
    assert exc.value.status == 415 and exc.value.message == NOT_HTML


def test_fetch_missing_content_type_is_tolerated():
    def handler(request):
        return httpx.Response(200, content=b"<html>x</html>")
    assert fetch_page("https://site.test/r", transport=transport(handler)).html == "<html>x</html>"


def test_fetch_oversize_is_413():
    big = "<html>" + "x" * (MAX_BYTES + 1) + "</html>"
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://site.test/r", transport=transport(lambda r: html_response(big)))
    assert exc.value.status == 413 and exc.value.message == TOO_LARGE


def test_fetch_connection_failure_is_502():
    def handler(request):
        raise httpx.ConnectError("getaddrinfo failed")
    with pytest.raises(RecipeError) as exc:
        fetch_page("https://nope.test/r", transport=transport(handler))
    assert exc.value.status == 502
    assert exc.value.message.startswith("Couldn't fetch that page: ") and "getaddrinfo failed" in exc.value.message
```

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_recipe.py -q`
Expected: ModuleNotFoundError `kroger_reorder.recipe`.

**Step 3: Implement** `kroger_reorder/recipe.py`

```python
# kroger_reorder/recipe.py
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
```

**Step 4: Run**

Run: `.venv/Scripts/python -m pytest tests/test_recipe.py -q`
Expected: 7 pass (parametrized counts as several).

**Step 5: Commit**

```bash
git add kroger_reorder/recipe.py tests/test_recipe.py
git commit -m "feat(recipe): fetch a recipe page with limits and clear errors"
```

---

### Task 3: `recipe.extract()`: JSON-LD ingredients with text fallback

**Files:**
- Modify: `kroger_reorder/recipe.py`
- Test: `tests/test_recipe.py`

**Step 1: Write the failing tests** (append; add `extract` to the import)

```python
# -- extract --------------------------------------------------------------------------

def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


RECIPE = {"@context": "https://schema.org", "@type": "Recipe", "name": "Chicken Tikka Masala",
          "recipeYield": ["4", "4 servings"],
          "recipeIngredient": ["2 lbs boneless chicken thighs", "1 cup plain yogurt", "  2 tbsp  garam masala "]}


def test_extract_reads_recipe_object():
    out = extract(f"<html><head><title>Site</title>{ld(RECIPE)}</head><body>junk</body></html>")
    assert out.title == "Chicken Tikka Masala" and out.yield_ == "4"
    assert out.ingredients == ["2 lbs boneless chicken thighs", "1 cup plain yogurt", "2 tbsp garam masala"]
    assert out.text == ""


def test_extract_finds_recipe_inside_graph_with_type_list_after_a_broken_block():
    doc = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "A page"},
        {"@type": ["Recipe", "NewsArticle"], "name": "Soup", "recipeIngredient": "1 onion"},
    ]}
    html = '<script type="application/ld+json">{not json</script>' + ld(doc)
    out = extract(html)
    assert out.title == "Soup" and out.ingredients == ["1 onion"] and out.yield_ == ""


def test_extract_skips_recipe_objects_without_ingredients():
    html = ld({"@type": "Recipe", "name": "Empty"}) + ld({"@type": "Recipe", "name": "Full", "recipeIngredient": ["salt"]})
    assert extract(html).title == "Full"


def test_extract_falls_back_to_visible_text_without_scripts_nav_or_tags():
    body = "<p>Ingredients</p><ul><li>2 eggs</li><li>1 cup flour</li></ul>" + "<p>more text</p>" * 30
    html = (f"<html><head><title>Pancakes | Site</title><style>p{{}}</style><script>var x=1;</script></head>"
            f"<body><nav>Home Recipes</nav><header>Logo</header>{body}<footer>© Site</footer></body></html>")
    out = extract(html)
    assert out.title == "Pancakes | Site" and out.ingredients == []
    assert "2 eggs 1 cup flour" in out.text
    for gone in ("var x=1", "p{}", "Home Recipes", "Logo", "© Site", "<li>"):
        assert gone not in out.text


def test_extract_with_too_little_text_is_422():
    with pytest.raises(RecipeError) as exc:
        extract("<html><body><p>Search results</p></body></html>")
    assert exc.value.status == 422 and exc.value.message == NO_RECIPE


def test_extract_truncates_long_text():
    html = "<html><body>" + "<p>word</p>" * 10000 + "</body></html>"
    assert len(extract(html).text) <= 20_000
```

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_recipe.py -q -k extract`
Expected: ImportError `extract`.

**Step 3: Implement** (add to `recipe.py`; add `import json`, `import re`, `from collections.abc import
Iterator`, `from html.parser import HTMLParser`, `from typing import Any`)

```python
MAX_TEXT = 20_000   # characters of page text handed to Claude when there is no JSON-LD recipe
MIN_TEXT = 200      # below this the page is a search page, a login wall, or an error


@dataclass
class Extracted:
    title: str
    yield_: str
    ingredients: list[str]  # from JSON-LD; empty when the page had no Recipe object
    text: str               # visible page text, only filled when `ingredients` is empty


class _Collector(HTMLParser):
    """One pass over the HTML: collects JSON-LD script blocks, the <title>, and the visible text."""
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_blocks: list[str] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._ld: list[str] | None = None
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").strip().lower() == "application/ld+json":
            self._ld = []
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.ld_blocks.append("".join(self._ld))
            self._ld = None
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)
        elif self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self.text_parts.append(data)


def _recipes(node: Any) -> Iterator[dict]:
    """Every object typed Recipe, wherever it sits: top level, a list, or nested under @graph etc."""
    if isinstance(node, list):
        for child in node:
            yield from _recipes(child)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "recipe" for t in types if t):
            yield node
        for child in node.values():
            if isinstance(child, (list, dict)):
                yield from _recipes(child)


def _clean(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("text") or ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lines(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [line for line in (_clean(v) for v in value) if line]


def extract(html: str) -> Extracted:
    parser = _Collector()
    parser.feed(html)
    parser.close()
    title = _clean("".join(parser.title_parts))
    for block in parser.ld_blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue  # a site's broken block shouldn't hide a good one
        for recipe in _recipes(data):
            ingredients = _lines(recipe.get("recipeIngredient"))
            if ingredients:
                return Extracted(_clean(recipe.get("name")) or title, _clean(recipe.get("recipeYield")),
                                 ingredients, "")
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()[:MAX_TEXT]
    if len(text) < MIN_TEXT:
        raise RecipeError(422, NO_RECIPE)
    return Extracted(title, "", [], text)
```

**Step 4: Run** `.venv/Scripts/python -m pytest tests/test_recipe.py -q` → all pass.

**Step 5: Commit**

```bash
git add kroger_reorder/recipe.py tests/test_recipe.py
git commit -m "feat(recipe): extract ingredients from JSON-LD with visible-text fallback"
```

---

### Task 4: `recipe.suggest()`: prompt, parsing, both backends

**Files:**
- Modify: `kroger_reorder/recipe.py`
- Test: `tests/test_recipe.py`

**Step 1: Write the failing tests** (append; import `KINDS, SYSTEM, Extracted, parse, suggest,
user_text` from `kroger_reorder.recipe`, `Item` from `kroger_reorder.storage`, `VisionError` from
`kroger_reorder.vision`, and reuse the helpers from `tests/test_vision.py` by importing them:
`from tests.test_vision import api_message, envelope, fake_client, fake_runner, which_ok` — check
`tests/__init__.py` exists; if not, create an empty one so the import works on every OS)

```python
# -- prompt, parse, suggest ---------------------------------------------------------------

EXTRACTED = Extracted("Chicken Tikka Masala", "4 servings", ["2 lbs chicken thighs", "1 cup yogurt"], "")
ITEMS = [Item(upc="1", name="Large Eggs", size="12 ct", quantity=1), Item(upc="2", name="Whole Milk", size="1 gal", quantity=2)]


def test_user_text_has_title_yield_ingredients_list_items_note_and_history():
    text = user_text(EXTRACTED, "double it", "Weekly", ITEMS, "- Whole Milk: 5x")
    for needle in ("Recipe: Chicken Tikka Masala", "Yield: 4 servings", "- 2 lbs chicken thighs", "- 1 cup yogurt",
                   "Items already on the list 'Weekly':", "- Large Eggs ×1", "- Whole Milk ×2",
                   "User's note: double it", "- Whole Milk: 5x"):
        assert needle in text, needle


def test_user_text_uses_page_text_and_empty_placeholder():
    page = Extracted("Pancakes", "", [], "Ingredients 2 eggs 1 cup flour ...")
    text = user_text(page, "", "Weekly", [], "")
    assert "Ingredients:" not in text and "Ingredients 2 eggs 1 cup flour" in text
    assert "(empty)" in text and "User's note: (none)" in text


def test_system_prompt_names_the_three_kinds():
    assert KINDS == ("needed", "staple", "on_list")
    for kind in KINDS:
        assert f'"{kind}"' in SYSTEM


def test_parse_uses_recipe_kinds_and_defaults_to_needed():
    out = parse(json.dumps([{"name": "chicken thighs", "quantity": 2, "kind": "needed", "reason": "r"},
                            {"name": "salt", "kind": "staple"}, {"name": "eggs", "kind": "on_list"},
                            {"name": "yogurt", "kind": "seen"}]))
    assert [(s.name, s.quantity, s.kind) for s in out] == [
        ("chicken thighs", 2, "needed"), ("salt", 1, "staple"), ("eggs", 1, "on_list"), ("yogurt", 1, "needed")]


def test_suggest_api_sends_prompt_without_images():
    make = fake_client(result=api_message(json.dumps([{"name": "chicken thighs", "kind": "needed"}])))
    out = suggest(EXTRACTED, "", "Weekly", ITEMS, "", model="m", timeout=5, api_key="k", make_client=make)
    assert [s.name for s in out] == ["chicken thighs"]
    (call,) = make.calls
    assert call["system"] == SYSTEM
    (block,) = call["messages"][0]["content"]
    assert block["type"] == "text" and "Large Eggs ×1" in block["text"]


def test_suggest_cli_runs_without_tools():
    run = fake_runner(stdout=envelope(json.dumps([{"name": "yogurt"}])))
    out = suggest(EXTRACTED, "", "Weekly", ITEMS, "", model="m", timeout=5, runner=run, which=which_ok)
    assert [s.name for s in out] == ["yogurt"]
    (args, stdin_text, _), = run.calls
    assert "--allowedTools" not in args and "Large Eggs ×1" in stdin_text and SYSTEM in stdin_text


def test_suggest_timeout_mentions_recipe_page():
    def slow(args, stdin_text, timeout):
        from kroger_reorder.vision import CliTimeout
        raise CliTimeout()
    with pytest.raises(VisionError) as exc:
        suggest(EXTRACTED, "", "Weekly", [], "", model="m", timeout=30, runner=slow, which=which_ok)
    assert exc.value.message == "Claude took longer than 30 seconds; try a shorter recipe page."
```

**Step 2: Run to verify they fail** → ImportError.

**Step 3: Implement** (append to `recipe.py`; add `import shutil`, `from collections.abc import Callable,
Iterator, Sequence`, `import anthropic`, `from .storage import Item`, and extend the vision import to
`from .vision import Runner, Suggestion, VisionError, Which, Wording, complete, context_text,
parse_suggestions, run_cli`)

```python
KINDS = ("needed", "staple", "on_list")

SYSTEM = """You turn a recipe's ingredient list into grocery store purchases for one household.

Reply with ONLY a JSON array, no prose and no code fence. Each element:
{"name": "<generic product name as you'd type into a grocery store search>", "quantity": <integer 1-10, number of store units to buy>, "kind": "needed" or "staple" or "on_list", "reason": "<one short sentence>"}

Rules:
- Combine lines that are the same product. Convert recipe amounts to store units: one carton of eggs, one bag of flour, one bunch of cilantro.
- "on_list": the shopping list already has this product (or a close equivalent). Say what the list has, for example "already on your list: Large Eggs ×1".
- "staple": salt, pepper, cooking oil, butter, flour, sugar, dried herbs and spices, vinegar, soy sauce, stock cubes, and similar things a kitchen keeps on hand, unless the note or the purchase history says the household is out.
- "needed": everything else.
- Honor the note: servings, substitutions, things to leave out, things the household already has.
- Skip water, ice, and garnish-only items. At most 40 items. Do not repeat items."""

RECIPE_WORDING = Wording(
    timeout="try a shorter recipe page",
    refusal="Claude declined to read that recipe; try a different page.",
    truncated="Claude's answer was cut off; try a recipe with fewer ingredients.",
)


def list_text(list_name: str, items: Sequence[Item]) -> str:
    lines = [f"- {item.name} ×{item.quantity}" for item in items] or ["(empty)"]
    return "\n".join([f"Items already on the list '{list_name}':"] + lines)


def user_text(page: Extracted, note: str, list_name: str, items: Sequence[Item], profile: str) -> str:
    parts = [f"Recipe: {page.title or '(untitled)'}"]
    if page.yield_:
        parts.append(f"Yield: {page.yield_}")
    if page.ingredients:
        parts += ["", "Ingredients:"] + [f"- {line}" for line in page.ingredients]
    else:
        parts += ["", "The page has no structured ingredient list. Find the recipe's ingredients in this page text:",
                  page.text]
    parts += ["", list_text(list_name, items), "", context_text(note, profile)]
    return "\n".join(parts)


def parse(result_text: str) -> list[Suggestion]:
    return parse_suggestions(result_text, kinds=KINDS)


def suggest(page: Extracted, note: str, list_name: str, items: Sequence[Item], profile: str, *, model: str,
            timeout: float, api_key: str | None = None,
            make_client: Callable[..., object] = anthropic.Anthropic,
            runner: Runner = run_cli, which: Which = shutil.which) -> list[Suggestion]:
    """Grocery suggestions for a recipe: API backend when `api_key` is given, otherwise the Claude Code CLI."""
    text = complete(SYSTEM, user_text(page, note, list_name, items, profile), wording=RECIPE_WORDING,
                    model=model, timeout=timeout, api_key=api_key, make_client=make_client,
                    runner=runner, which=which)
    return parse(text)
```

**Step 4: Run** `.venv/Scripts/python -m pytest -q` → all pass.

**Step 5: Commit**

```bash
git add kroger_reorder/recipe.py tests/test_recipe.py tests/__init__.py
git commit -m "feat(recipe): prompt and parsing for grocery suggestions with on-list and staple kinds"
```

---

### Task 5: Route `POST /api/recipe/suggest` and startup wiring

**Files:**
- Modify: `kroger_reorder/main.py` (imports, request model, `create_app` signature, photo route's line
  building, new route)
- Modify: `kroger_reorder/__main__.py`
- Test: `tests/test_routes.py`

**Step 1: Write the failing tests** (append to `tests/test_routes.py`; add
`from kroger_reorder.recipe import Extracted, Page, RecipeError`)

```python
# -- recipe suggestions ---------------------------------------------------------------------

RECIPE_HTML = ('<html><head><title>Site</title><script type="application/ld+json">'
               '{"@type":"Recipe","name":"Tikka Masala","recipeYield":"4","recipeIngredient":["2 lbs chicken","1 cup yogurt"]}'
               '</script></head><body></body></html>')


@pytest.fixture
def recipe_client(storage, kroger):
    calls = {"fetch": [], "suggest": []}

    def fetch(url):
        calls["fetch"].append(url)
        return Page(url=url, html=RECIPE_HTML)

    def suggester(page, note, list_name, items, profile, *, model, timeout):
        calls["suggest"].append({"page": page, "note": note, "list_name": list_name, "items": list(items),
                                 "profile": profile, "model": model, "timeout": timeout})
        return [Suggestion("chicken thighs", 2, "needed", "Two pounds"), Suggestion("large eggs", 1, "on_list", "already on your list: Eggs ×1")]
    app = create_app(storage, kroger, photo_model="test-model", photo_timeout=7, page_fetcher=fetch,
                     recipe_suggester=suggester)
    client = TestClient(app, follow_redirects=False)
    client.calls = calls
    return client


def test_recipe_suggest_fetches_extracts_and_passes_list_items(recipe_client, storage):
    storage.save_list("standard", "Standard", [Item(upc="0002", name="Eggs", size="12 ct", quantity=1)])
    push_one(recipe_client)
    r = recipe_client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard", "note": "double it"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Tikka Masala"
    assert r.json()["lines"] == [
        {"raw": "needed: Two pounds", "name": "chicken thighs", "quantity": 2, "exact": None, "kind": "needed", "reason": "Two pounds"},
        {"raw": "on_list: already on your list: Eggs ×1", "name": "large eggs", "quantity": 1, "exact": None, "kind": "on_list", "reason": "already on your list: Eggs ×1"},
    ]
    assert recipe_client.calls["fetch"] == ["https://site.test/r"]
    (call,) = recipe_client.calls["suggest"]
    assert isinstance(call["page"], Extracted) and call["page"].ingredients == ["2 lbs chicken", "1 cup yogurt"]
    assert call["note"] == "double it" and call["list_name"] == "Standard"
    assert [i.name for i in call["items"]] == ["Eggs"]
    assert "Whole Milk" in call["profile"] and call["model"] == "test-model" and call["timeout"] == 7


def test_recipe_suggest_unknown_list_is_404(recipe_client):
    r = recipe_client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "nope"})
    assert r.status_code == 404 and not recipe_client.calls["fetch"]


def test_recipe_suggest_passes_fetch_and_claude_errors_through(storage, kroger):
    def refused(url):
        raise RecipeError(502, "That site refused the request (HTTP 403). Copy the ingredients from the page and use Import instead.")
    client = TestClient(create_app(storage, kroger, page_fetcher=refused))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 502 and r.json()["error"].startswith("That site refused the request (HTTP 403)")

    def broken(*a, **k):
        raise VisionError(503, "Photos can't be analyzed: set ANTHROPIC_API_KEY in .env, or install Claude Code on the computer running the app.")
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, RECIPE_HTML), recipe_suggester=broken))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 503


def test_recipe_suggest_without_recipe_on_page_is_422(storage, kroger):
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, "<html><body>nothing</body></html>"),
                                   recipe_suggester=lambda *a, **k: []))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 422 and r.json()["error"].startswith("Couldn't find a recipe on that page.")


def test_recipe_suggest_with_no_suggestions_is_422(storage, kroger):
    client = TestClient(create_app(storage, kroger, page_fetcher=lambda url: Page(url, RECIPE_HTML),
                                   recipe_suggester=lambda *a, **k: []))
    r = client.post("/api/recipe/suggest", json={"url": "https://site.test/r", "slug": "standard"})
    assert r.status_code == 422 and r.json()["error"] == "Claude couldn't find any ingredients on that page."
```

`push_one` already exists in the file (it records an order so the usage profile has Whole Milk).

**Step 2: Run to verify they fail** → TypeError: unexpected keyword `page_fetcher`.

**Step 3: Implement**

`kroger_reorder/main.py`:

- Imports: `from .recipe import extract as extract_recipe, fetch_page, suggest as suggest_recipe`.
- Request model next to `PhotoRequest`:

```python
class RecipeRequest(BaseModel):
    url: str
    slug: str
    note: str = ""
```

- `create_app` signature gains `page_fetcher=fetch_page, recipe_suggester=suggest_recipe` after
  `analyzer=analyze_photos`; extend the docstring: "`page_fetcher(url) -> recipe.Page` and
  `recipe_suggester(...)` are the recipe equivalents of `analyzer`."
- Factor the line dict the photo route builds into a helper placed in the helpers section:

```python
    def suggestion_line(s) -> dict:
        return {"raw": f"{s.kind}: {s.reason}" if s.reason else s.kind, "name": s.name,
                "quantity": s.quantity, "exact": None, "kind": s.kind, "reason": s.reason}
```

  and use it in `photo_suggest` (`lines = [suggestion_line(s) for s in suggestions]`).
- New route right after `photo_suggest`, and rename the section comment to
  `# -- usage profile, photo and recipe suggestions ---`:

```python
    @app.post("/api/recipe/suggest")
    def recipe_suggest(body: RecipeRequest):
        loaded = get_list(body.slug)          # 404 before any network call
        page = page_fetcher(body.url)
        extracted = extract_recipe(page.html)
        profile = profile_text(usage_profile(storage.list_orders()))
        suggestions = recipe_suggester(extracted, body.note, loaded.name, loaded.items, profile,
                                       model=photo_model, timeout=photo_timeout)
        lines = [suggestion_line(s) for s in suggestions]
        if not lines:
            return error(422, "Claude couldn't find any ingredients on that page.")
        return {"title": extracted.title, "lines": lines}
```

`RecipeError` subclasses `VisionError`, so the existing `vision_error` handler answers it; no new
handler. Update that handler's docstring/comment if any to say it covers recipe errors too.

`kroger_reorder/__main__.py`:

- `from . import recipe` (or `from .recipe import suggest as suggest_recipe`).
- Pass `recipe_suggester=partial(suggest_recipe, api_key=settings.anthropic_api_key)` to `create_app`.
- Banner line `Photos:` becomes `Claude:` (same `PHOTO_LABELS`; rename the dict to `CLAUDE_LABELS`).
  Run `grep -rn "Photos:" tests/` first; if a test asserts the banner text, update it.

**Step 4: Run** `.venv/Scripts/python -m pytest -q` → all pass except `tests/test_docs.py`, which now
fails on the undocumented route and module. That is expected until Task 7; do not touch the README yet.

**Step 5: Commit**

```bash
git add kroger_reorder/main.py kroger_reorder/__main__.py tests/test_routes.py
git commit -m "feat(api): POST /api/recipe/suggest turns a recipe URL into review lines"
```

---

### Task 6: Recipe dialog and Skip-preselected review rows

No automated tests cover the page (there is no JS test runner; see AGENTS.md). Verify by hand with the
steps below.

**Files:**
- Modify: `kroger_reorder/static/index.html`
- Modify: `kroger_reorder/static/app.js`

**Step 1: index.html**

- Button row (next to `data-action="photo"`):

```html
        <button class="secondary" data-action="recipe" title="Paste a recipe link and get the groceries it needs">Recipe</button>
```

- Styles, after `.kind.suggested`:

```css
  .kind.needed { background:#e4f5ea; color:var(--ok); }
  .kind.staple { background:#eef0f3; color:#555; }
  .kind.on_list { background:#e8ecff; color:#2b3fa8; }
```

  Check how `input[type=text]`/`textarea` are styled in the page (`grep -n "input\[" index.html`) and
  give `#recipe-url` the same width treatment so it spans the dialog.

- Dialog, after `#photo-dialog`:

```html
<dialog id="recipe-dialog">
  <h2>Groceries from a recipe</h2>
  <p class="muted">Paste the link to a recipe. Claude reads its ingredient list and proposes what to buy, skipping what your list already has and the pantry staples a kitchen keeps. You review every line before it's added.</p>
  <p id="recipe-backend" class="muted"></p>
  <div class="row">
    <label class="muted" for="recipe-list">Add to</label>
    <select id="recipe-list"></select>
  </div>
  <input id="recipe-url" type="url" placeholder="https://www.example.com/recipes/chicken-tikka-masala" style="margin-top:10px">
  <textarea id="recipe-note" rows="2" placeholder="Anything to add? e.g. double it, skipping the sauce, we already have chicken"></textarea>
  <div id="recipe-banner" class="banner"></div>
  <div class="row">
    <button id="recipe-go" disabled>Suggest groceries</button>
    <button id="recipe-close" class="secondary">Cancel</button>
  </div>
</dialog>
```

**Step 2: app.js**

- Action dispatch: add `} else if (action === 'recipe') { openRecipe();` next to the `photo` branch.
- Factor two helpers out of `openPhoto()` and use them there too:

```js
// The "Add to" picker shared by the Photo and Recipe dialogs: every list plus "New list…".
function fillListPicker(sel) {
  sel.innerHTML = '';
  state.lists.forEach(l => {
    const opt = document.createElement('option');
    opt.value = l.slug; opt.textContent = l.name; opt.selected = l.slug === state.active_list;
    sel.appendChild(opt);
  });
  const fresh = document.createElement('option');
  fresh.value = '__new__'; fresh.textContent = 'New list…';
  sel.appendChild(fresh);
}

function backendText(what) {
  return {
    api: `${what} analyzed through the Anthropic API key in .env.`,
    cli: `${what} analyzed by Claude Code on the computer running the app.`,
  }[state.photo_backend] || `Claude isn't set up yet: add ANTHROPIC_API_KEY to .env, or install Claude Code on the computer running the app.`;
}

// Resolve the picker's choice to a slug, creating the list when "New list…" was chosen. null = cancelled.
async function pickTargetList(sel) {
  let slug = sel.value;
  if (slug === '__new__') {
    const name = prompt('Name for the new list:');
    if (!name?.trim()) return null;
    ({ slug } = await api('POST', '/api/lists', { name }));
    await refreshLists();
  }
  return slug;
}
```

  `openPhoto()` calls `fillListPicker($('photo-list'))` and sets `#photo-backend` to
  `backendText('Photos are')`; the photo submit handler uses `pickTargetList($('photo-list'))`.

- Recipe dialog, after the photo section:

```js
// -- recipe suggestions ------------------------------------------------------------------------

function openRecipe() {
  $('recipe-url').value = '';
  $('recipe-note').value = '';
  $('recipe-go').disabled = true;
  banner(null, '', false, $('recipe-banner'));
  $('recipe-backend').textContent = backendText('Recipes are');
  fillListPicker($('recipe-list'));
  $('recipe-dialog').showModal();
  $('recipe-url').focus();
}

$('recipe-close').onclick = () => $('recipe-dialog').close();
$('recipe-url').oninput = () => { $('recipe-go').disabled = !$('recipe-url').value.trim(); };
$('recipe-url').onkeydown = (e) => { if (e.key === 'Enter' && !$('recipe-go').disabled) $('recipe-go').click(); };

$('recipe-go').onclick = async () => {
  const rbanner = $('recipe-banner');
  const url = $('recipe-url').value.trim();
  if (!url) return;
  $('recipe-go').disabled = true;
  try {
    const slug = await pickTargetList($('recipe-list'));
    if (!slug) return;
    banner('warn', 'Fetching the recipe and asking Claude… this takes 10 to 30 seconds.', false, rbanner);
    const { title, lines } = await api('POST', '/api/recipe/suggest', { url, slug, note: $('recipe-note').value });
    if (slug !== state.active_list) await switchList(slug);
    $('recipe-dialog').close();
    openImport();
    banner('warn', `Claude suggested ${plural(lines.length, 'item')} for “${title || 'the recipe'}”. Matching them to your store…`, false, $('import-banner'));
    await reviewLines(lines, $('import-banner'));
  } catch (err) {
    banner('error', err.message, false, $('recipe-dialog').open ? rbanner : $('import-banner'));
  } finally {
    $('recipe-go').disabled = !$('recipe-url').value.trim();
  }
};
```

- `renderImport()`: rows whose kind is `staple` or `on_list` start on Skip, and the tag shows "on list"
  rather than "on_list":

```js
    const skip = row.kind === 'staple' || row.kind === 'on_list';
    const options = row.candidates.map((c, ci) =>
      `<option value="${ci}" ${ci === 0 && !skip ? 'selected' : ''}>…unchanged…</option>`
    ).join('');
    const kind = row.kind ? `<span class="kind ${esc(row.kind)}">${esc(row.kind.replace('_', ' '))}</span>` : '';
    …
      <td><select data-import-pick="${idx}">${options}<option value="-1" ${row.candidates.length && !skip ? '' : 'selected'}>${row.candidates.length ? 'Skip this line' : 'No match found — skip'}</option></select></td>`;
```

**Step 3: Verify by hand** (do not use the live server on port 8000 or the `data/` folder):

```powershell
$env:KROGER_REORDER_DATA = "$env:TEMP\kroger-recipe-check"; $env:PORT = "8765"
.venv\Scripts\python -m kroger_reorder --no-browser
```

Open http://localhost:8765. Without a store chosen, the review step will fail at the catalog lookup
with "Pick a store first", which is fine for checking the dialog. Confirm: Recipe button opens the
dialog; the hint line names the backend; Suggest is disabled until a URL is typed; a bad address shows
the 400 message in the dialog; Cancel closes. Stop the server and delete the temp folder.

**Step 4: Commit**

```bash
git add kroger_reorder/static/index.html kroger_reorder/static/app.js
git commit -m "feat(ui): Recipe dialog; on-list and staple rows start on Skip in the review"
```

---

### Task 7: Documentation, version 6.0.0, drift guard green

**Files:**
- Modify: `README.md`, `AGENTS.md`, `.env.example`, `kroger_reorder/__init__.py`
- Test: `tests/test_docs.py` (no change; it must pass)

**Step 1: Run the drift guard** → `.venv/Scripts/python -m pytest tests/test_docs.py -q` fails on the
route and the module. Keep it as the checklist.

**Step 2: README.md**

- **Features**: after the Photo suggestions bullet:
  `- **Recipe suggestions.** Paste the link to a recipe and Claude turns its ingredient list into store purchases, flagging what the chosen list already has and the pantry staples a kitchen keeps (both preset to Skip). Same backends and review step as photos.`
- **Project structure**: a line for `recipe.py` after `vision.py`:
  `│   ├── recipe.py          Recipe URL suggestions: fetch, JSON-LD ingredients, Claude normalization`.
  Also change the `vision.py` description to `Claude backends (Anthropic API or headless Claude Code) and photo suggestions`.
- **Getting started › 5. Optional: photo suggestions**: rename the heading to
  `### 5. Optional: photo and recipe suggestions` and update the ToC link and any in-page anchors
  (`grep -n "optional-photo-suggestions" README.md`). First sentence: both features share the setup.
- **Using the app**: after `### Photo suggestions` add:

```markdown
### Recipe suggestions

Click **Recipe**, choose which list the groceries should go to (or **New list…**), paste the recipe's web address, add a note if you like ("double it", "skipping the sauce", "we already have chicken"), and click **Suggest groceries**.

The computer running the app fetches the page, reads the recipe's ingredient list (most recipe sites publish one in a machine-readable form; otherwise the page text is used), and hands it to Claude together with the items already on that list and your usage profile. After 10 to 30 seconds the import review table opens with every ingredient tagged **needed**, **staple** (salt, oil, flour, spices and similar; Skip is preselected), or **on list** (the list already has it, with what it has in the reason; Skip is preselected). Change any dropdown to add a staple you are out of, then **Add to list** as for any import. Nothing about the recipe is stored.

Pages that only show ingredients after running scripts, and sites that block automated visitors, cannot be read; copy the ingredients from the page into **Import** instead.
```

- **HTTP API › Photo suggestions and usage**: rename to `### Photo and recipe suggestions, usage` and add
  `| `POST` | `/api/recipe/suggest` | A recipe URL, the target list slug, and a note; returns the recipe title and suggested lines for the import review |`.
- **Privacy sentence** (the one mentioning photos sent to Anthropic): add "and, for recipe suggestions,
  the ingredient lines, the chosen list's item names, and your note".
- **Troubleshooting**: rows, strings verbatim from `recipe.py`/`main.py`:

| Symptom | Fix |
|---|---|
| `Enter a full web address starting with http:// or https://.` | Paste the whole link from the browser's address bar. |
| `That site refused the request (HTTP 403)...` | The site blocks automated visitors. Copy the ingredients from the page into **Import**. |
| `Couldn't fetch that page: ...` | The computer running the app could not reach the site; check the address and its network. |
| `That address isn't a web page.` | The link points at a PDF, image or file. Use the recipe page's own link. |
| `Couldn't find a recipe on that page.` | Search results, category pages and pages that draw ingredients with scripts have nothing to read. Open the recipe itself, or use **Import**. |
| `Claude couldn't find any ingredients on that page.` | The page text had no recognizable recipe. Use **Import** with the ingredients copied from the page. |

  Also reword the `Photos can't be analyzed...` row's symptom cell to "after **Suggest a list** or
  **Suggest groceries**".
- **Version history**: `| 2026-10-29 | v6 | Recipe URL suggestions: paste a recipe link, Claude proposes the groceries, on-list and staple rows start on Skip |`.
- **Adding a feature** checklist and **Manual smoke test**: add a recipe line to the smoke test
  ("Recipe → paste a link → rows tagged needed / staple / on list → Add to list").

**Step 3: `.env.example`**: the `ANTHROPIC_API_KEY` comment starts "Optional. Photo and recipe
suggestions." and the `PHOTO_MODEL` / `PHOTO_TIMEOUT` comments say "photo and recipe suggestions".
Update the README **Configuration** table descriptions of those three settings the same way.

**Step 4: `AGENTS.md`**: Layout table row for `kroger_reorder/recipe.py` ("Recipe URL suggestions:
fetch, JSON-LD extraction, prompt; shares `vision.complete()`"); the `vision.py` row becomes "Shared
Claude call (`complete()`: Anthropic SDK or Claude Code CLI) and photo suggestions"; in "Conventions
and traps" add: "`recipe.fetch_page()` must never hit the network in tests: pass `transport=` an
`httpx.MockTransport`." Add to "Things deliberately not done": "Importing raw ingredient lines without
a Claude backend; scaling servings automatically (the note covers it)."

**Step 5: Version**: `kroger_reorder/__init__.py` → `__version__ = "6.0.0"`.

**Step 6: Run the full suite** → `.venv/Scripts/python -m pytest -q`, all green including
`tests/test_docs.py`.

**Step 7: Commit**

```bash
git add README.md AGENTS.md .env.example kroger_reorder/__init__.py
git commit -m "docs: recipe suggestions in README and AGENTS.md; version 6.0.0"
```

---

### Task 8: Final review, merge, push, CI

1. Dispatch a final code review over `git diff main...recipe-url`.
2. Fix anything it finds with the usual test-first loop.
3. `git checkout main && git merge --ff-only recipe-url && git push origin main`.
4. Watch CI: `gh run watch` (or `gh run list --limit 1`); all seven jobs must pass.
5. Hand-check against the real backend from the maintainer's machine after restarting the Startup
   shortcut server, with a real recipe URL, before calling it done.
