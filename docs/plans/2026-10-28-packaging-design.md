# Packaging and redeployment (v5) — design

**Goal.** Someone who finds the repository on GitHub can run the app on Windows, macOS, Linux, or
Docker in a few minutes, with every feature working, including photo suggestions through their own
Anthropic API key. Jake's own setup (Windows, venv, Claude Code on his subscription, Start Menu and
startup shortcuts) keeps working unchanged apart from a one-time `pip install -e .`.

**Audience decision.** Any-OS self-hoster. Not PyPI (no publish step, but the package is shaped so
that it could be later).

## 1. Photo suggestions: API key or Claude Code

`vision.analyze()` keeps its signature and its `VisionError` contract. Behind it there are two
backends chosen at startup:

| `ANTHROPIC_API_KEY` set? | Backend | Who it's for |
|---|---|---|
| yes | **API**: `anthropic` SDK, `client.messages.create` with the photos as base64 `image` blocks | Self-hosters, Docker, Mac/Linux |
| no | **CLI**: `claude -p` subprocess, exactly as today | Jake and anyone on a Claude subscription |

- The prompt is shared. The CLI backend keeps "Use the Read tool to look at each of these photos" plus
  the paths; the API backend sends the images inline and drops that line. `SYSTEM` becomes the API
  `system` parameter; note and purchase history are the user text block after the images.
- API call: `model=settings.photo_model`, `max_tokens=8192`, no `thinking` parameter (adaptive is the
  default on current models), `timeout=settings.photo_timeout`, `max_retries=1`. The text blocks of
  the reply are joined and go through the existing `parse_suggestions()`. The server-side refusal
  fallback beta is **not** enabled: grocery photos don't trip safety classifiers, and the app should
  depend only on the stable API surface. A `stop_reason == "refusal"` becomes a 502 "Claude declined
  to analyze these photos."
- Default `PHOTO_MODEL` changes from `claude-opus-5` to `claude-opus-5-5` for both backends (current
  Opus, cheaper). Claude Code accepts the same id.
- Errors map to the existing statuses: `AuthenticationError`/`PermissionDeniedError` → 502 "Anthropic
  rejected ANTHROPIC_API_KEY"; `RateLimitError` → 503 "Anthropic rate limit; wait a minute";
  `APITimeoutError` → 504 (same wording as the CLI timeout); `APIConnectionError` and other
  `APIStatusError` → 502 with the SDK message; no key *and* no `claude` on PATH → 503 "Set
  ANTHROPIC_API_KEY in .env, or install Claude Code on this computer."
- Testability: the API backend takes an injectable client factory (`make_client=anthropic.Anthropic`);
  tests pass a fake whose `messages.create` returns a stub message or raises SDK exception classes
  constructed directly. No test touches the network.
- The existing `GET /api/state` gains `photo_backend: "api" | "cli" | null` so the page can show
  which one is active in the photo dialog's hint line. Nothing else in the UI changes.

## 2. Package rename: `app` → `kroger_reorder`

`app/` becomes `kroger_reorder/` with `__version__ = "4.0.0"` in `__init__.py`. `pyproject.toml`
reads the version dynamically from it. Every `from app.` import, `python -m app`, the two PowerShell
scripts, the README, and the docstrings follow. `python -m kroger_reorder` and the `kroger-reorder`
console script are the two documented ways to start it. The old `app` name is not kept as an alias.

## 3. Configurable paths and URLs

| Variable | Default | Purpose |
|---|---|---|
| `KROGER_REORDER_DATA` | `./data` relative to the current directory | Where lists, config, tokens, and orders live. The Windows shortcuts already set the working directory to the checkout, so Jake's data stays put. Docker sets it to `/data`. |
| `PUBLIC_URL` | `http://localhost:<PORT>` | Origin the browser uses to reach the app. The OAuth redirect becomes `<PUBLIC_URL>/callback`, so a headless box can register `http://nas.local:8000/callback` on the Kroger developer portal. `--launcher` and the auto-opened browser tab keep using localhost. |
| `ANTHROPIC_API_KEY` | unset | Enables the API photo backend. |

`.env` is found from the working directory first (`find_dotenv(usecwd=True)`), then from the package
location as before. `load_settings()` validates `PUBLIC_URL` (must start with `http://` or
`https://`, trailing slash stripped) and keeps the existing checks.

**Headless sign-in without `PUBLIC_URL`.** Documented fallback: `ssh -L 8000:localhost:8000 box` from
a PC, then sign in at `http://localhost:8000`. Tokens are stored server-side, so it's a one-time step.

## 4. Docker

- `Dockerfile`: `python:3.13-slim`, copy `pyproject.toml` + package, `pip install .`, `ENV
  KROGER_REORDER_DATA=/data`, `EXPOSE 8000`, `CMD ["kroger-reorder", "--no-browser"]`. No Node, no
  Claude Code; photos require `ANTHROPIC_API_KEY` in the container.
- `docker-compose.yml`: one service, `env_file: .env`, `ports: "8000:8000"`, `volumes:
  ./data:/data`, `restart: unless-stopped`.
- `.dockerignore`: `.venv`, `data`, `.git`, `docs`, `img`, `tests`, caches.
- The LAN-address banner still prints inside the container (it shows the container's address, which
  is wrong behind a port mapping); the README tells Docker users to use `PUBLIC_URL` and ignore it.
  Not worth code.

## 5. Reproducible installs

- `pyproject.toml` gains `anthropic>=1.0` in `dependencies`, plus `readme`, `license`, `authors`,
  `classifiers`, and `[project.urls]` (Homepage, Issues).
- `requirements.lock`: exact versions from `pip freeze` on the dev venv (without the editable line),
  regenerated by hand when dependencies change. README offers it as the "exactly what the author
  runs" path; `pip install .` stays the default.

## 6. CI

`.github/workflows/ci.yml`: on push and pull request, matrix of `ubuntu-latest`, `windows-latest`,
`macos-latest` × Python `3.12`, `3.13`; `pip install -e ".[dev]"`; `pytest -q`. A badge goes at the
top of the README.

## 7. README

Rewrite **Getting started** as: clone → choose one of three paths (Windows venv, macOS/Linux venv,
Docker) → register the Kroger app → fill `.env` → run. New subsections: *Photo suggestions: API key
or Claude Code*, *Running on another machine* (`PUBLIC_URL`, SSH tunnel), *Configuration* table of
every env var. Update Technology (add `anthropic`), Project structure (new package name, new files),
Troubleshooting (API-key errors), Version history (v5 row), and the test count.

## Out of scope

Publishing to PyPI or a container registry; an auto-updater; TLS; multi-user auth; hiding the Photo
button when no backend is configured (the 503 message is enough).

## Testing

- `tests/test_vision.py`: both backends through fakes; backend selection by key presence; each API
  error class → status; refusal stop reason; prompt differences (paths vs inline images).
- `tests/test_settings.py`: `PUBLIC_URL` default and override, invalid value exits, redirect URI
  derivation, data dir from env.
- `tests/test_storage.py`: `Storage(data_dir)` already injectable; add one test that the default
  resolves from the environment.
- `tests/test_routes.py`: `/api/state` reports the backend.
- Manual: Docker build and run on this PC with an API key; sign-in through the mapped port; Windows
  shortcuts still work after the rename; `pip install .` into a fresh venv on Windows runs with
  `cd` to an empty folder and `KROGER_REORDER_DATA` set.
