# Recipe URL suggestions (v6) — design

**Goal.** Paste the web address of a recipe and the app proposes the groceries it needs, skipping
what the chosen list already has and what a kitchen normally keeps, then lets the user review every
line against the store catalog before it is added. Same backends as photo suggestions (own Anthropic
API key, or Claude Code on the host), no new settings, no new dependencies.

**Decisions made with Jake (2026-10-29).**

- An ingredient already on the list is shown in the review with Skip preselected and a reason such
  as "already on your list: Large Eggs ×1". It is never dropped silently and never auto-bumped.
- Pantry staples (salt, pepper, oil, flour, dried spices) are shown with Skip preselected too; one
  click on the dropdown adds any the household is out of.

## 1. Approaches considered

| | Approach | Verdict |
|---|---|---|
| 1 | **Server fetches and extracts the ingredient lines; Claude normalizes them** into grocery items with the list and purchase history in hand | **Chosen.** Deterministic fetch, small prompt, reuses both Claude backends and their error handling. |
| 2 | Claude fetches the page itself (web-fetch tool) | CLI backend would need network tool permission, many recipe sites block automated fetchers, and every call pays for the whole HTML. |
| 3 | No Claude: regex parser that strips quantities and units | Brittle on real lines ("2 cups cooked, shredded chicken"), cannot judge staples or coverage by the current list. |

## 2. Architecture

```
browser                      server                                   Claude
Recipe dialog ──POST /api/recipe/suggest {url, slug, note}──▶ recipe.fetch_page()  (httpx)
                                                              recipe.extract()     (JSON-LD → ingredients)
                                                              vision.complete()  ──prompt──▶ API or CLI
                                                              recipe.parse()    ◀──JSON array──
              ◀── {title, lines:[{name, quantity, kind, reason}]} ──
import review table (existing) ──POST /api/import/resolve, POST /api/lists/{slug}/import (existing)
```

### 2.1 `kroger_reorder/recipe.py` (new)

- `fetch_page(url, *, transport=None) -> Page`:
  - URL must parse with scheme `http` or `https` and a host, else `RecipeError(400, "Enter a full web
    address starting with http:// or https://.")`.
  - `httpx.Client(follow_redirects=True, timeout=15.0, headers={"User-Agent": <browser-like>,
    "Accept": "text/html,…"})`. The transport is injectable for tests.
  - Response must be `text/html` (or `application/xhtml+xml`), else 415 "That address isn't a web
    page. Paste the link to the recipe itself."
  - Body capped at 2 MB (streamed; stop reading past the cap), else 413 "That page is too large to
    read."
  - HTTP 4xx/5xx → 502 "That site refused the request (HTTP 403). Copy the ingredients from the page
    and use Import instead." Connection failures and timeouts → 502 "Couldn't fetch that page:
    <reason>. Check the address and try again."
- `extract(html) -> Extracted(title, yield_, ingredients, text)`:
  - Every `<script type="application/ld+json">` block is parsed; objects are walked through `@graph`
    and arrays; an object whose `@type` is `Recipe` (or a list containing it) yields `name`,
    `recipeYield`, and `recipeIngredient` (list of strings, or a single string). Malformed JSON blocks
    are skipped.
  - When no Recipe object exists, `text` holds the page's visible text: `<script>`, `<style>`,
    `<nav>`, `<header>`, `<footer>` dropped, tags stripped with `html.parser`, whitespace collapsed,
    truncated to 20 000 characters. `title` comes from `<title>`.
  - Neither ingredients nor meaningful text (under 200 characters) → 422 "Couldn't find a recipe on
    that page. Try the recipe's own page rather than a search or category page."
- `RecipeError(status, message)` mirrors `VisionError`.
- `suggest(page, note, list_items, profile, *, model, timeout, api_key, make_client, runner, which)
  -> Suggested(title, items)` builds the prompt and calls `vision.complete()`; `parse()` reads the
  JSON array with kinds `needed | staple | on_list`.

### 2.2 `kroger_reorder/vision.py` (refactor, behavior unchanged for photos)

The API-or-CLI dispatch and the SDK error mapping are pulled out of `analyze_api`/`analyze_cli` into
one generic call:

- `complete(system, user_text, *, images=(), model, timeout, api_key, make_client, runner, which,
  cli_tools=()) -> str` returns the model's text. API backend: `system` + content blocks (images
  first, then text). CLI backend: prompt = user text + system, `--allowedTools` only when `cli_tools`
  is non-empty (photos need `Read`; recipes need nothing, so the CLI runs with no tools).
- `_extract_json_array` and `parse_suggestions` stay; `parse_suggestions` gains a `kinds=KINDS`
  parameter and the recipe module passes its own.
- Photo-specific wording stays photo-specific: the timeout, refusal, and `max_tokens` messages take a
  `hint` so photos keep "try fewer or smaller photos" and recipes say "try a shorter recipe page".
  Every string quoted in the README Troubleshooting table keeps its current text.
- `analyze()` (photos) keeps its signature; `__main__.py` and `main.py` do not change for photos.

### 2.3 Prompt

System prompt (recipe):

> You turn a recipe's ingredient list into grocery store purchases for one household. Reply with ONLY
> a JSON array, no prose, no code fence. Each element: {"name": "<generic product name as typed into
> a grocery search>", "quantity": <integer 1-10, number of store units to buy>, "kind": "needed" |
> "staple" | "on_list", "reason": "<one short sentence>"}.
> Rules: combine lines that are the same product; convert recipe amounts to store units (one carton
> of eggs, one bag of flour); "on_list" when the shopping list already has it, and say what it has;
> "staple" for salt, pepper, cooking oil, flour, sugar, dried herbs and spices, vinegar, and similar
> pantry items a kitchen keeps, unless the note or purchase history says otherwise; "needed" for
> everything else; honor the note (servings, substitutions, things to leave out); skip water and
> garnish-only items; at most 40 items.

User text: recipe title and yield, the ingredient lines (or the page text when there are none),
"Items already on the list '<name>':" with `name ×quantity` per item (or "(empty)"), the note, and the
purchase history text already produced for photos.

### 2.4 Route and page

- `POST /api/recipe/suggest` body `{url, slug, note}` → `{title, lines}`. `slug` must name an
  existing list (404 as elsewhere); its items go to Claude. Lines have the same shape as photo lines
  (`raw`, `name`, `quantity`, `exact: null`, `kind`, `reason`) so `reviewLines()` needs no change.
  `create_app()` gains `recipe_suggester=` and `page_fetcher=` injectables next to `analyzer=`.
- `__main__.py` wires `recipe_suggester=partial(recipe.suggest, api_key=…)` with the same model and
  timeout as photos; the banner's "Photos via" line becomes "Claude via" since both features share it.
- `index.html`: a **Recipe** button beside **Photo**; `#recipe-dialog` with a URL input, the
  **Add to** list select (same options as the photo dialog, factored into a helper), a note
  textarea, a banner, **Suggest groceries** and **Cancel**. Hint line reuses the backend text.
- `app.js`: `openRecipe()`, submit handler mirroring the photo one (resolve "New list…" first, then
  POST, switch list, open import review). `renderImport()` preselects the Skip option when
  `row.kind` is `staple` or `on_list`; `.kind.staple` and `.kind.on_list` get their own colors.
  `updateImportSummary()` already counts only non-skipped rows.

### 2.5 Errors

| Where | Status | Message (quoted in README) |
|---|---|---|
| bad URL | 400 | `Enter a full web address starting with http:// or https://.` |
| site 4xx/5xx | 502 | `That site refused the request (HTTP 403). Copy the ingredients from the page and use Import instead.` |
| network | 502 | `Couldn't fetch that page: …` |
| not HTML | 415 | `That address isn't a web page. Paste the link to the recipe itself.` |
| over 2 MB | 413 | `That page is too large to read.` |
| no recipe | 422 | `Couldn't find a recipe on that page. Try the recipe's own page rather than a search or category page.` |
| Claude found nothing | 422 | `Claude couldn't find any ingredients on that page.` |
| Claude / backend | as today | existing `VisionError` messages |

## 3. Testing

- `tests/test_recipe.py`: URL validation; fetch through `httpx.MockTransport` (redirect followed,
  403, non-HTML, oversize, connection error); extraction from JSON-LD as a single object, inside
  `@graph`, with a list `@type`, with a string `recipeIngredient`, with one malformed block among
  good ones; text fallback strips scripts and nav; prompt includes list items, note, yield and
  ingredients; `parse()` honors the three kinds and clamps quantities; `suggest()` through a fake
  client and a fake CLI runner.
- `tests/test_vision.py`: existing photo tests pass unchanged after the refactor; one test shows the
  CLI gets no `--allowedTools` when no tools are requested.
- `tests/test_routes.py`: fake fetcher plus fake suggester; the suggester receives the target list's
  items and the note; 404 for an unknown slug; `RecipeError` and `VisionError` statuses pass through;
  empty suggestions → 422.
- `tests/test_docs.py` enforces the README rows for the route, the module, the design doc, and v6.

## 4. Out of scope

- Scaling servings automatically (the note covers "double it").
- Saving recipes or linking a list back to its source URL.
- Sites that render ingredients only with JavaScript: the text fallback sees nothing; the error
  points the user at Import.
- A no-Claude fallback that imports raw ingredient lines. Photos require a backend; recipes do too.
- Blocking fetches of private or local addresses. The operator supplies the URL on a single-user LAN
  app; a hostile link could make the server GET its own `/callback`, which is why the design keeps
  the app off the public internet (see README security note).
