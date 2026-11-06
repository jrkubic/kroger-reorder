# Working on Kroger Reorder

Read this before changing anything. It is for humans and for coding agents alike. The README is the
user manual; this file is the contributor manual.

## What this is

A single-household, self-hosted FastAPI + vanilla-JS app that fills a Kroger pickup cart from saved
grocery lists. Python 3.12+, no JavaScript build step, JSON files for storage. Everything a user needs
is in [README.md](README.md); everything a maintainer needs is here.

## Get running in two minutes

```bash
git clone https://github.com/jrkubic/kroger-reorder.git && cd kroger-reorder
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # Windows: .venv\Scripts\python
.venv/bin/python -m pytest -q                   # must pass before and after your change
cp .env.example .env                            # fill KROGER_CLIENT_ID / KROGER_CLIENT_SECRET to run the app
.venv/bin/python -m kroger_reorder              # http://localhost:8000
```

Tests need no `.env`, no network, and never touch `data/`. If a test needs the Kroger API, mock it
with `httpx.MockTransport` as `tests/test_kroger.py` does; if it needs Claude, inject a fake as
`tests/test_vision.py` does.

## Layout

| Path | What lives there |
|---|---|
| `kroger_reorder/main.py` | FastAPI app factory `create_app()` and every HTTP route |
| `kroger_reorder/kroger.py` | Kroger API client: OAuth, locations, products, cart |
| `kroger_reorder/aldi.py` | Aldi US client: replays aldi.us's own GraphQL calls (search, items, store) and fills the signed-in cart (`account`, `fill_cart`) |
| `kroger_reorder/storage.py` | JSON persistence; `Storage(data_dir)` is the only way in |
| `kroger_reorder/stores.py` | The stores a list can belong to: Kroger (catalog, cart), Aldi (catalog, linked cart) and the hand-off stores Meijer, Walmart (search links) |
| `kroger_reorder/settings.py` | `.env` / environment parsing; every setting has a default here |
| `kroger_reorder/vision.py` | Shared Claude call (`complete()`: Anthropic SDK or Claude Code CLI) and photo suggestions |
| `kroger_reorder/recipe.py` | Recipe URL suggestions: fetch, JSON-LD extraction, prompt; shares `vision.complete()` |
| `kroger_reorder/receipt.py` | Reads a pasted Kroger receipt (text or screenshots) through `vision.complete()` |
| `kroger_reorder/importer.py`, `orders.py`, `usage.py` | Import parsing, order snapshots + Kroger cart status / checkout merge / receipt outcomes, usage profile |
| `kroger_reorder/static/` | `index.html` (page + styles) and `app.js` (all page logic) |
| `kroger_reorder/__main__.py` | Entry point, startup banner, Windows headless logging |
| `tests/` | One `test_<module>.py` per module, plus `test_docs.py` (README drift guard) |
| `docs/plans/` | One `<date>-<topic>-design.md` and one `<date>-<topic>.md` plan per version |
| `scripts/` | Windows Startup and Start Menu shortcut installers |

## How a feature ships

1. **Design first.** Write `docs/plans/<date>-<topic>-design.md`: goal, approaches considered,
   chosen architecture, data flow, error handling, testing. Short is fine. Get it agreed before code.
2. **Plan.** Write `docs/plans/<date>-<topic>.md` with bite-sized tasks, each with the failing test,
   the implementation, and the commit.
3. **TDD per task.** Failing test, minimal code, green suite, commit. Commits follow
   [Conventional Commits](https://www.conventionalcommits.org/): `feat(scope):`, `fix:`, `docs:`,
   `refactor:`, `build:`, `ci:`, `chore:`.
4. **Update the README in the same change.** `tests/test_docs.py` fails CI if you add any of these
   without documenting it:
   - a route in `main.py` → the matching table under **HTTP API**
   - a setting in `.env.example` → the **Configuration** table (and `.env.example` itself must
     describe it)
   - a module in `kroger_reorder/` → **Project structure**
   - a design doc in `docs/plans/` → **Design documents**
   - a version bump in `kroger_reorder/__init__.py` → a **Version history** row (the newest `vN`
     must match the major of `__version__`)

   Not enforced but expected: **Features** bullet, **Using the app** subsection, **Troubleshooting**
   rows for any new user-facing error string (copy the string verbatim from the source), and the
   Technology table if a dependency changes. Screenshots in `img/` when the UI changes visibly.
5. **Version.** Bump `__version__` when the change is a new numbered version in the README sense
   (user-visible feature set), not for fixes.
6. **Verify, then push.** Full suite green locally, push to `main` (or open a PR), and watch the CI
   run: Ubuntu, Windows, macOS × Python 3.12/3.13 plus a Docker build.

## Conventions and traps

- `Storage(data_dir)` is required; the `./data` default lives in `settings.DEFAULT_DATA_DIR` and is
  relative to the working directory. The Windows shortcuts set the working directory to the checkout.
- `create_app()` takes everything injectable (`storage`, `kroger`, `analyzer`, `photo_backend`,
  `public_url`). Tests build it with fakes; never read `.env` inside a route.
- User-facing error strings are part of the contract: the README Troubleshooting table quotes them.
  Change one, change both.
- The page is served with `Cache-Control: no-cache` and a version-stamped script URL. Do not add a
  bundler or framework.
- `vision.py` must never call the network in tests: pass `make_client=` (API) or `runner=`/`which=`
  (CLI) fakes. The real `anthropic.Anthropic` is only reached when `api_key` is set and no fake is
  given.
- `recipe.fetch_page()` must never hit the network in tests: pass `transport=` an `httpx.MockTransport`.
- Every list has a `store` (`stores.PROVIDERS` key; missing in the file means Kroger). Each provider has
  `catalog` (search, matching, prices) and `cart` (Send to cart) flags: Kroger both, Aldi catalog only,
  Meijer and Walmart neither. Routes use `has_catalog(p)` (catalog flag and a client available) and
  `p.cart`. Plain items are allowed on every list without a cart and are keyed by `stores.item_key(name)`
  in the `upc` field; catalog products keep their numeric id there, which is why `importer._exact` only
  treats an all-digit `upc` as a product.
- `aldi.AldiClient` must never hit the network in tests: pass `transport=` an `httpx.MockTransport`
  (see `tests/test_aldi.py`, which serves trimmed real responses from `tests/fixtures/aldi_*`). Every
  failure is an `AldiError`; the page falls back to plain rows on Aldi lists. When aldi.us changes, the
  persisted-query hashes are the usual culprit: update `SEARCH_HASH` / `ITEMS_HASH` / `LOCATION_HASH`
  (users can override them with `ALDI_*_HASH` in `.env`), refresh the fixtures, and check
  `parse_context` still finds the store context in the search page.
- The Aldi cart (v7.2) uses the user's pasted aldi.us session cookie, kept in `aldi_session.json` in the
  data folder. It is a sign-in: never log it, never return it from an API (the page sees name, email and
  `connected_at`), and only `fill_cart` / `account` may send it, each on its own httpx client so the
  anonymous catalog session never carries it. `Provider.linked_cart` marks a store whose cart works this
  way; `cart` still means Kroger's API cart, so an Aldi list stays a hand-off list (plain items, Open at
  Aldi) and `push` dispatches on `has_linked_cart(p)` first. Orders carry `cart` (`kroger` or `aldi`;
  missing means Kroger) and reorder dispatches on it. aldi.us *sets* cart quantities, so `fill_cart`
  sends `max(in cart, wanted)`.
- Kroger orders carry `status` (v7.3): `in_cart` from the send until **I checked out** (`ordered`) or
  **I emptied my cart** (`dropped`); no status means ordered (older orders, Aldi). Only ordered orders
  feed `usage_profile`, and an item's receipt `outcome` overrides its quantity (out of stock and
  refunded are skipped). Every Kroger send, push or reorder, goes through `plan_kroger_send`: overlap
  with in-cart products is a 409 that saves nothing until the page resends with `mode`. Checkout
  replaces the in-cart sends with one merged order, so receipts match one real Kroger order. Tests
  that need purchases use `push_one`, which checks out after the send.
- `data/` under the checkout is the maintainer's live grocery data. Tests use `tmp_path`; manual
  Docker checks mount a scratch directory, never `./data`.
- `.env` holds real credentials. Never print it, never commit it, never copy it into an image.
- Windows line endings: the repo stores LF; `core.autocrlf` warnings on commit are expected.

## Things deliberately not done

- Digital coupon clipping: Kroger's public API has none; a kroger.com scraper was considered and
  postponed.
- Reading the real Kroger cart, order history or loyalty points: the public API has none, and
  kroger.com doesn't answer a plain HTTP client. The app tracks only its own sends and pasted receipts;
  a browser extension on kroger.com is the unprobed alternative.
- Multi-user auth, TLS, PyPI publishing, a container registry image, a non-root Docker user.
- Hiding the Photo button when no backend is configured: the dialog explains instead.
- Importing raw ingredient lines without a Claude backend; scaling servings automatically (the note
  covers it).
- Filling Meijer or Walmart carts, or scraping meijer.com or walmart.com. Aldi's catalog is read (v7.1)
  and its cart filled through the user's own pasted sign-in (v7.2), Jake's decisions for a personal tool;
  the app never signs in to aldi.us itself (its login has a captcha, deliberately not bypassed) and
  never checks out. A bookmarklet run on aldi.us is the fallback if pasted cookies turn out to expire
  within hours. Walmart catalog matching through the walmart.io affiliate API is planned once a key is
  granted.
- Changing a list's store after creation (export, create, import instead).
