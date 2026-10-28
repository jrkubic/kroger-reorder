# Packaging and Redeployment (v5) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the repository runnable by a stranger on Windows, macOS, Linux, or Docker, with photo suggestions working through either an Anthropic API key or a local Claude Code install.

**Architecture:** The `app` package becomes `kroger_reorder` with a single version source. `settings.py` grows three environment variables (`PUBLIC_URL`, `KROGER_REORDER_DATA`, `ANTHROPIC_API_KEY`). `vision.py` gains an API backend beside the existing CLI backend, chosen by whether a key is present. Docker, CI, a lock file, and a README rewrite wrap it up.

**Tech Stack:** Python 3.12+, FastAPI, `anthropic` SDK 1.x/2.x (built on `httpx2`, coexists with the project's `httpx`), pytest, Docker, GitHub Actions.

Design: `docs/plans/2026-10-28-packaging-design.md`.

**Conventions for every task**
- Run tests with `.venv\Scripts\python -m pytest -q` from `C:\Projects\kroger-reorder`.
- Commit messages follow Conventional Commits and end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Never print `.env`. It holds real credentials.
- The user's live data is in `C:\Projects\kroger-reorder\data`. Tests use `tmp_path`; manual Docker checks use a scratch directory, never `./data`.

---

### Task 1: Rename the package to `kroger_reorder` and fix package metadata

**Files:**
- Move: `app/` → `kroger_reorder/`
- Modify: `kroger_reorder/__init__.py`, `pyproject.toml`, `scripts/install-startup.ps1`, `scripts/install-start-menu.ps1`, every `tests/*.py`, header comments in `kroger_reorder/*.py`, `kroger_reorder/static/app.js:1`, `kroger_reorder/__main__.py` docstring
- `.gitignore` (no change needed; `*.egg-info/` already covers the new name)

**Step 1: Move the package and rewrite imports**

```bash
cd /c/Projects/kroger-reorder
git mv app kroger_reorder
# imports in tests
sed -i 's/^from app\./from kroger_reorder./' tests/*.py
# header comments like "# app/main.py" and "# tests/test_x.py" references
sed -i 's|^# app/|# kroger_reorder/|' kroger_reorder/*.py
sed -i 's|talks to the JSON API in app/main.py|talks to the JSON API in kroger_reorder/main.py|' kroger_reorder/static/app.js
# scripts
sed -i "s/-m app --no-browser/-m kroger_reorder --no-browser/; s/\`-m app --no-browser\`/\`-m kroger_reorder --no-browser\`/" scripts/install-startup.ps1
sed -i "s/-m app --launcher/-m kroger_reorder --launcher/" scripts/install-start-menu.ps1
grep -rn "python -m app\|-m app\b\|from app\.\|app/__main__" kroger_reorder tests scripts README.md docs/plans/2026-10-28-packaging-design.md || echo "no stale references"
```

Expected: the final grep prints only README lines (fixed in Task 11) or nothing.

**Step 2: Add the version to `kroger_reorder/__init__.py`**

```python
"""Kroger Reorder: fill a Kroger pickup cart from saved grocery lists."""
__version__ = "4.0.0"
```

**Step 3: Update the `__main__.py` docstring**

Replace the first docstring line `` `python -m app` (or `kroger-reorder`): start the server for the whole LAN and open the browser. `` with `` `python -m kroger_reorder` (or `kroger-reorder`): start the server for the whole LAN and open the browser. `` and `` `python -m app --no-browser` `` with `` `python -m kroger_reorder --no-browser` ``.

**Step 4: Rewrite `pyproject.toml`**

```toml
[project]
name = "kroger-reorder"
dynamic = ["version"]
description = "Self-hosted web app that fills your Kroger pickup cart from saved grocery lists"
readme = "README.md"
license = "MIT"
license-files = ["LICENSE"]
authors = [{ name = "Jake Kubicki" }]
requires-python = ">=3.12"
classifiers = [
  "Environment :: Web Environment",
  "Framework :: FastAPI",
  "Intended Audience :: End Users/Desktop",
  "Operating System :: OS Independent",
  "Programming Language :: Python :: 3",
  "Topic :: Home Automation",
]
dependencies = [
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "httpx>=0.27",
  "python-dotenv>=1.0",
  "anthropic>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.urls]
Homepage = "https://github.com/jrkubic/kroger-reorder"
Issues = "https://github.com/jrkubic/kroger-reorder/issues"

[project.scripts]
kroger-reorder = "kroger_reorder.__main__:main"

[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["kroger_reorder"]

[tool.setuptools.dynamic]
version = { attr = "kroger_reorder.__version__" }

[tool.setuptools.package-data]
kroger_reorder = ["static/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`license = "MIT"` as a plain string needs setuptools 77+, hence the bumped build requirement.

**Step 5: Reinstall editable and run the suite**

```bash
rm -rf kroger_reorder.egg-info
.venv/Scripts/python -m pip install -q -e ".[dev]"
.venv/Scripts/python -c "import kroger_reorder; print(kroger_reorder.__version__)"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m kroger_reorder --help
```

Expected: `4.0.0`, `183 passed`, and the argparse help text with `prog` still `kroger-reorder`.

**Step 6: Commit**

```bash
git add -A
git commit -m "refactor: rename package app -> kroger_reorder; version and metadata in pyproject

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Settings for `PUBLIC_URL`, `KROGER_REORDER_DATA`, `ANTHROPIC_API_KEY`

**Files:**
- Modify: `kroger_reorder/settings.py`
- Test: `tests/test_settings.py`
- Modify: `.env.example`

**Step 1: Write the failing tests** (append to `tests/test_settings.py`; also change the existing photo default assertion)

```python
from pathlib import Path

BASE = {"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec"}


def test_public_url_defaults_to_localhost_and_can_be_overridden():
    s = load_settings(env=BASE)
    assert s.public_url == "http://localhost:8000"
    s = load_settings(env={**BASE, "PUBLIC_URL": "http://nas.local:8000/"})
    assert s.public_url == "http://nas.local:8000"
    assert s.redirect_uri == "http://nas.local:8000/callback"


def test_public_url_must_be_http_or_https():
    with pytest.raises(SystemExit) as info:
        load_settings(env={**BASE, "PUBLIC_URL": "nas.local:8000"})
    assert "PUBLIC_URL" in str(info.value) and "http://" in str(info.value)


def test_data_dir_defaults_to_cwd_data_and_reads_env():
    assert load_settings(env=BASE).data_dir == Path("data").resolve()
    s = load_settings(env={**BASE, "KROGER_REORDER_DATA": "/srv/kroger"})
    assert s.data_dir == Path("/srv/kroger").resolve()


def test_anthropic_api_key_is_optional_and_stripped():
    assert load_settings(env=BASE).anthropic_api_key is None
    assert load_settings(env={**BASE, "ANTHROPIC_API_KEY": "  "}).anthropic_api_key is None
    assert load_settings(env={**BASE, "ANTHROPIC_API_KEY": " sk-x "}).anthropic_api_key == "sk-x"
```

In `test_photo_defaults_and_overrides` change `"claude-opus-5"` to `"claude-opus-5-5"`.

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_settings.py -q`
Expected: 5 failures (`AttributeError: public_url`, `data_dir`, `anthropic_api_key`, and the model default).

**Step 3: Implement**

Replace `kroger_reorder/settings.py` with:

```python
"""Reads configuration from .env / environment and refuses to start without Kroger credentials."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

REQUIRED = ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET")

DEFAULT_PHOTO_MODEL = "claude-opus-5-5"
DEFAULT_PHOTO_TIMEOUT = 90.0
DEFAULT_DATA_DIR = "data"


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    port: int
    public_url: str                      # origin browsers use to reach the app; the OAuth redirect hangs off it
    data_dir: Path                       # lists, config, tokens, orders
    photo_model: str = DEFAULT_PHOTO_MODEL   # model for photo suggestions (API or Claude Code)
    photo_timeout: float = DEFAULT_PHOTO_TIMEOUT  # seconds before a photo analysis is abandoned
    anthropic_api_key: str | None = None  # set -> photos go to the Anthropic API; unset -> Claude Code CLI

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/callback"

    @property
    def local_url(self) -> str:
        return f"http://localhost:{self.port}"


def _load_dotenv_files() -> None:
    """Prefer a .env in the working directory (how the scripts and Docker run it), else the one next to
    the package (a source checkout run from elsewhere)."""
    path = find_dotenv(usecwd=True) or find_dotenv()
    if path:
        load_dotenv(path)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env`, or from .env + os.environ when `env` is None."""
    if env is None:
        _load_dotenv_files()
        env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        sys.exit(
            f"Missing {', '.join(missing)}.\n"
            "Copy .env.example to .env and fill in the values from your app at https://developer.kroger.com."
        )
    raw_port = env.get("PORT") or "8000"
    try:
        port = int(raw_port)
    except ValueError:
        sys.exit(f"PORT must be a number, got '{raw_port}'.")
    public_url = (env.get("PUBLIC_URL") or f"http://localhost:{port}").strip().rstrip("/")
    if not public_url.startswith(("http://", "https://")):
        sys.exit(f"PUBLIC_URL must start with http:// or https://, got '{public_url}'.")
    raw_timeout = env.get("PHOTO_TIMEOUT") or str(DEFAULT_PHOTO_TIMEOUT)
    try:
        photo_timeout = float(raw_timeout)
    except ValueError:
        sys.exit(f"PHOTO_TIMEOUT must be a number of seconds, got '{raw_timeout}'.")
    api_key = (env.get("ANTHROPIC_API_KEY") or "").strip() or None
    return Settings(
        client_id=env["KROGER_CLIENT_ID"],
        client_secret=env["KROGER_CLIENT_SECRET"],
        port=port,
        public_url=public_url,
        data_dir=Path(env.get("KROGER_REORDER_DATA") or DEFAULT_DATA_DIR).resolve(),
        photo_model=(env.get("PHOTO_MODEL") or DEFAULT_PHOTO_MODEL).strip(),
        photo_timeout=photo_timeout,
        anthropic_api_key=api_key,
    )
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_settings.py -q`
Expected: all pass.

**Step 5: Update `.env.example`**

```
# Required. From your app at https://developer.kroger.com (My Apps -> your app).
KROGER_CLIENT_ID=
KROGER_CLIENT_SECRET=

# Optional. Port to listen on. The Kroger app's redirect URL must match: <PUBLIC_URL>/callback
PORT=8000

# Optional. How browsers reach this server when it runs on another machine (a NAS, a Pi, Docker on a
# server). Register <PUBLIC_URL>/callback as the redirect URL on your Kroger app. Leave unset when the
# app runs on the PC you sign in from (default http://localhost:<PORT>).
#PUBLIC_URL=http://nas.local:8000

# Optional. Folder for lists, config, tokens, and order history. Default: ./data
#KROGER_REORDER_DATA=./data

# Optional. Photo suggestions. Set an Anthropic API key (https://console.anthropic.com) and photos are
# analyzed through the API, pay-per-use. Leave unset and the app runs Claude Code (`claude -p`) on this
# computer instead, on its signed-in subscription.
#ANTHROPIC_API_KEY=

# Optional. Model for photo suggestions, used by both backends.
PHOTO_MODEL=claude-opus-5-5
# Optional. Seconds to wait for a photo analysis before giving up.
PHOTO_TIMEOUT=90
```

**Step 6: Commit**

```bash
git add kroger_reorder/settings.py tests/test_settings.py .env.example
git commit -m "feat(settings): PUBLIC_URL, KROGER_REORDER_DATA, ANTHROPIC_API_KEY; .env found from cwd

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Storage default is the working directory's `data/`

> **Superseded during implementation (Task 5):** `Storage(data_dir)` is now a required argument, the `DATA_DIR` constant is gone, and no storage default test exists. The `./data` default lives in `settings.DEFAULT_DATA_DIR`. The version string shipped as `5.0.0`, not `4.0.0`.

**Files:**
- Modify: `kroger_reorder/storage.py:14`
- Test: `tests/test_storage.py`

**Step 1: Write the failing test** (append)

```python
def test_default_data_dir_is_relative_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert Storage().data_dir == Path("data")
```

Add `from pathlib import Path` to the test imports.

**Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_storage.py::test_default_data_dir_is_relative_to_cwd -q`
Expected: FAIL, the default is the checkout's absolute `data` path.

**Step 3: Implement**

In `kroger_reorder/storage.py` replace

```python
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
```

with

```python
DATA_DIR = Path("data")  # relative to the working directory; __main__ passes settings.data_dir explicitly
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass (every other test constructs `Storage(tmp_path)`).

**Step 5: Commit**

```bash
git add kroger_reorder/storage.py tests/test_storage.py
git commit -m "refactor(storage): default data dir is ./data under the working directory

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: API photo backend in `vision.py`

**Files:**
- Modify: `kroger_reorder/vision.py`
- Test: `tests/test_vision.py`

**Step 1: Write the failing tests** (append to `tests/test_vision.py`; add imports)

Add to the imports at the top:

```python
import base64
from types import SimpleNamespace

import anthropic
import httpx2

from kroger_reorder.vision import SYSTEM, choose_backend, context_text
```

Append:

```python
# -- API backend ----------------------------------------------------------------------

def api_message(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def api_error(cls, status, message="boom"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


def fake_client(result=None, error=None):
    """A stand-in for anthropic.Anthropic: records constructor kwargs and messages.create kwargs."""
    created, calls = [], []

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return result

    def make_client(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=create))
    make_client.created = created
    make_client.calls = calls
    return make_client


@pytest.fixture
def jpg_photos(tmp_path):
    a, b = tmp_path / "photo-1.jpg", tmp_path / "photo-2.png"
    a.write_bytes(b"\xff\xd8\xff jpeg bytes")
    b.write_bytes(b"\x89PNG png bytes")
    return [a, b]


def test_context_text_has_note_and_history_with_placeholders():
    assert "User's note: out of eggs" in context_text("out of eggs", "- Milk: 3x")
    assert "- Milk: 3x" in context_text("out of eggs", "- Milk: 3x")
    text = context_text("  ", "")
    assert "User's note: (none)" in text and "(no orders yet)" in text


def test_choose_backend_prefers_api_key_then_cli():
    assert choose_backend(api_key="sk-x", which=lambda n: None) == "api"
    assert choose_backend(api_key=None, which=which_ok) == "cli"
    assert choose_backend(api_key=None, which=lambda n: None) is None


def test_analyze_api_sends_inline_images_and_parses_text(jpg_photos):
    make = fake_client(result=api_message(json.dumps(GOOD)))
    out = analyze(jpg_photos, "out of eggs", "- Milk: 3x", model="claude-opus-5-5", timeout=42,
                  api_key="sk-test", make_client=make, which=lambda n: None, runner=fake_runner())
    assert [s.name for s in out] == ["whole milk", "large eggs"]
    assert make.created == [{"api_key": "sk-test", "timeout": 42, "max_retries": 1}]
    (call,) = make.calls
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 8192 and call["system"] == SYSTEM
    (msg,) = call["messages"]
    assert msg["role"] == "user"
    images, (text,) = msg["content"][:2], msg["content"][2:]
    assert [i["source"]["media_type"] for i in images] == ["image/jpeg", "image/png"]
    assert images[0]["source"]["data"] == base64.standard_b64encode(jpg_photos[0].read_bytes()).decode()
    assert text["type"] == "text" and "out of eggs" in text["text"] and "- Milk: 3x" in text["text"]
    assert "Read tool" not in text["text"]


@pytest.mark.parametrize("error, status, needle", [
    (api_error(anthropic.AuthenticationError, 401), 502, "ANTHROPIC_API_KEY"),
    (api_error(anthropic.PermissionDeniedError, 403), 502, "ANTHROPIC_API_KEY"),
    (api_error(anthropic.RateLimitError, 429), 503, "rate limit"),
    (api_error(anthropic.APIStatusError, 500, "overloaded"), 502, "overloaded"),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), 504, "longer than 42"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), 502, "reach Anthropic"),
])
def test_analyze_api_maps_sdk_errors(jpg_photos, error, status, needle):
    with pytest.raises(VisionError) as exc:
        analyze(jpg_photos, "", "", model="m", timeout=42, api_key="sk", make_client=fake_client(error=error))
    assert exc.value.status == status and needle in exc.value.message


def test_analyze_api_refusal_is_502(jpg_photos):
    make = fake_client(result=api_message("", stop_reason="refusal"))
    with pytest.raises(VisionError) as exc:
        analyze(jpg_photos, "", "", model="m", timeout=5, api_key="sk", make_client=make)
    assert exc.value.status == 502 and "declined" in exc.value.message


def test_analyze_without_key_or_cli_explains_both_options():
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=fake_runner(), which=lambda n: None)
    assert exc.value.status == 503
    assert "ANTHROPIC_API_KEY" in exc.value.message and "Claude Code" in exc.value.message
```

Also update `test_analyze_missing_cli_is_503`: it still passes unchanged (status only).

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_vision.py -q`
Expected: ImportError on `choose_backend`/`context_text`.

**Step 3: Implement**

Replace `kroger_reorder/vision.py` with:

```python
# kroger_reorder/vision.py
"""Turn photos of a fridge, pantry or cupboard into grocery suggestions.

Two backends, chosen by configuration:

- **API** (`ANTHROPIC_API_KEY` set): the Anthropic SDK, photos sent inline as base64 image blocks.
  Pay-per-use; works anywhere, including Docker.
- **CLI** (no key): the installed `claude` CLI run headlessly (`claude -p … --output-format json`),
  which reads the photos with its Read tool. Uses the host's Claude subscription.

Both runners are injectable so tests never call the network or start Claude Code.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anthropic

MAX_ITEMS = 40
MAX_QUANTITY = 10
KINDS = ("seen", "suggested")
API_MAX_TOKENS = 8192
API_MAX_RETRIES = 1
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

SYSTEM = """You are helping build a grocery shopping list from photos of a household's fridge, freezer, pantry or cupboard, together with the user's note and their purchase history.

Reply with ONLY a JSON array, no prose and no code fence. Each element:
{"name": "<generic product name as you'd type into a grocery store search>", "quantity": <integer 1-10>, "kind": "seen" or "suggested", "reason": "<one short sentence>"}

Include:
- items visible in the photos that a household would re-buy ("seen");
- items that look empty, nearly empty, or are clearly absent but implied by the photos, the note, or the purchase history ("suggested"), for example staples bought regularly that are overdue;
- anything the note asks for.

Prefer plain names like "whole milk" or "large eggs" over brands unless a brand is clearly visible. Skip non-food household items unless visible or asked for. At most 40 items. Do not repeat items."""

NO_BACKEND = ("Photos can't be analyzed: set ANTHROPIC_API_KEY in .env, or install Claude Code on the "
              "computer running the app.")


class VisionError(Exception):
    """A human-readable failure; `status` is the HTTP status the API should answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Suggestion:
    name: str
    quantity: int
    kind: str
    reason: str


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], str, float], RunResult]
Which = Callable[[str], str | None]


def choose_backend(*, api_key: str | None, which: Which = shutil.which) -> str | None:
    """'api' when a key is configured, 'cli' when `claude` is on PATH, else None."""
    if api_key:
        return "api"
    if which("claude"):
        return "cli"
    return None


def context_text(note: str, profile: str) -> str:
    return "\n".join([
        "User's note: " + (note.strip() or "(none)"),
        "",
        "Purchase history (most bought first):",
        profile.strip() or "(no orders yet)",
    ])


# -- shared parsing ---------------------------------------------------------------------

def _extract_json_array(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("not a JSON array")
    return data


def parse_suggestions(result_text: str) -> list[Suggestion]:
    try:
        data = _extract_json_array(result_text)
    except ValueError as exc:
        raise VisionError(502, "Claude's answer couldn't be read; try again.") from exc
    out, seen_names = [], set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        try:
            quantity = int(entry.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1
        kind = str(entry.get("kind") or "seen").lower()
        out.append(Suggestion(
            name=name,
            quantity=min(MAX_QUANTITY, max(1, quantity)),
            kind=kind if kind in KINDS else "seen",
            reason=str(entry.get("reason") or "").strip(),
        ))
        if len(out) >= MAX_ITEMS:
            break
    return out


# -- API backend ------------------------------------------------------------------------

def _image_block(path: Path) -> dict:
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        raise VisionError(415, f"Unsupported photo type {path.suffix}.")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def analyze_api(photos: list[Path], note: str, profile: str, *, model: str, timeout: float, api_key: str,
                make_client: Callable[..., object] = anthropic.Anthropic) -> list[Suggestion]:
    client = make_client(api_key=api_key, timeout=timeout, max_retries=API_MAX_RETRIES)
    content = [_image_block(p) for p in photos] + [{"type": "text", "text": context_text(note, profile)}]
    try:
        message = client.messages.create(
            model=model,
            max_tokens=API_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise VisionError(502, "Anthropic rejected ANTHROPIC_API_KEY; check the key in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise VisionError(503, "Anthropic rate limit reached; wait a minute and try again.") from exc
    except anthropic.APITimeoutError as exc:
        raise VisionError(504, f"Claude took longer than {int(timeout)} seconds; try fewer or smaller photos.") from exc
    except anthropic.APIConnectionError as exc:
        raise VisionError(502, "Couldn't reach Anthropic; check the network on the computer running the app.") from exc
    except anthropic.APIStatusError as exc:
        raise VisionError(502, f"Anthropic error: {exc.message}") from exc
    if getattr(message, "stop_reason", None) == "refusal":
        raise VisionError(502, "Claude declined to analyze these photos; try different ones.")
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    return parse_suggestions(text)


# -- CLI backend ------------------------------------------------------------------------

def run_cli(args: list[str], stdin_text: str, timeout: float) -> RunResult:
    """Run the CLI with the prompt on stdin. On Windows `claude` is a .CMD shim, and cmd.exe cuts a
    multi-line argument at the first newline, so the prompt must never travel as an argument."""
    try:
        proc = subprocess.run(args, input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VisionError(504, f"Claude took longer than {int(timeout)} seconds; try fewer or smaller photos.") from exc
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def build_prompt(photos: list[Path], note: str, profile: str) -> str:
    lines = ["Use the Read tool to look at each of these photos:"]
    lines += [f"- {p}" for p in photos]
    lines += ["", context_text(note, profile), "", SYSTEM]
    return "\n".join(lines)


def analyze_cli(photos: list[Path], note: str, profile: str, *, model: str, timeout: float,
                runner: Runner = run_cli, which: Which = shutil.which) -> list[Suggestion]:
    exe = which("claude")
    if not exe:
        raise VisionError(503, NO_BACKEND)
    args = [exe, "-p", "--output-format", "json", "--allowedTools", "Read", "--max-turns", "6"]
    if model:
        args += ["--model", model]
    result = runner(args, build_prompt(photos, note, profile), timeout)
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
    return parse_suggestions(str(envelope.get("result") or ""))


# -- entry point ------------------------------------------------------------------------

def analyze(photos: list[Path], note: str, profile: str, *, model: str, timeout: float,
            api_key: str | None = None, make_client: Callable[..., object] = anthropic.Anthropic,
            runner: Runner = run_cli, which: Which = shutil.which) -> list[Suggestion]:
    """API backend when `api_key` is given, otherwise the Claude Code CLI."""
    if api_key:
        return analyze_api(photos, note, profile, model=model, timeout=timeout, api_key=api_key,
                           make_client=make_client)
    return analyze_cli(photos, note, profile, model=model, timeout=timeout, runner=runner, which=which)
```

**Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_vision.py -q`
Expected: all pass, including the pre-existing CLI tests.

**Step 5: Commit**

```bash
git add kroger_reorder/vision.py tests/test_vision.py
git commit -m "feat(vision): Anthropic API backend when ANTHROPIC_API_KEY is set; Claude Code CLI otherwise

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Wire the backend and URLs through `/api/state` and the entry point

**Files:**
- Modify: `kroger_reorder/main.py:118-121` (`create_app` signature), `:215-219` (`/api/state`)
- Modify: `kroger_reorder/__main__.py`
- Test: `tests/test_routes.py`

**Step 1: Write the failing tests**

In `tests/test_routes.py`, change `test_initial_state_has_default_list` to expect the two new keys:

```python
def test_initial_state_has_default_list(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json() == {
        "store": None, "signed_in": False,
        "lists": [{"slug": "standard", "name": "Standard", "count": 0}],
        "active_list": "standard",
        "photo_backend": None, "public_url": "http://localhost:8000",
    }
```

Append:

```python
def test_state_reports_photo_backend_and_public_url(storage, kroger):
    app = create_app(storage, kroger, photo_backend="api", public_url="http://nas.local:8000")
    s = TestClient(app).get("/api/state").json()
    assert s["photo_backend"] == "api" and s["public_url"] == "http://nas.local:8000"
```

**Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_routes.py -q -k "state"`
Expected: 2 failures.

**Step 3: Implement in `main.py`**

Change the `create_app` signature and docstring:

```python
def create_app(storage: Storage, kroger, *, photo_model: str = "claude-opus-5-5", photo_timeout: float = 90.0,
               analyzer=analyze_photos, photo_backend: str | None = None,
               public_url: str = "http://localhost:8000") -> FastAPI:
    """`kroger` is a KrogerClient or anything with the same methods (tests pass a fake).
    `analyzer` has the signature of vision.analyze; tests pass a fake so nothing external is called.
    `photo_backend` ('api' | 'cli' | None) and `public_url` are reported to the page via /api/state."""
```

Change the state route:

```python
    @app.get("/api/state")
    def state():
        config = storage.load_config()
        store = {"store_id": config["store_id"], "store_name": config.get("store_name", "")} if config.get("store_id") else None
        return {"store": store, "signed_in": kroger.is_signed_in(), "photo_backend": photo_backend,
                "public_url": public_url, **lists_payload()}
```

**Step 4: Implement in `__main__.py`**

Replace the imports and `main()`:

```python
import argparse
import os
import socket
import sys
import threading
import webbrowser
from functools import partial
from pathlib import Path

import uvicorn

from . import __version__
from .kroger import KrogerClient
from .main import create_app
from .settings import load_settings
from .storage import Storage
from .vision import analyze, choose_backend

PHOTO_LABELS = {
    "api": "Anthropic API (ANTHROPIC_API_KEY)",
    "cli": "Claude Code on this computer",
    None: "off (set ANTHROPIC_API_KEY in .env, or install Claude Code)",
}
```

and

```python
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kroger-reorder", description="Run the Kroger Reorder server.")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in a browser at startup")
    parser.add_argument("--launcher", action="store_true",
                        help="Start Menu behaviour: if the server is already running just open the page, else start it")
    parser.add_argument("--version", action="version", version=f"kroger-reorder {__version__}")
    args = parser.parse_args(argv)
    attach_log_when_headless()

    settings = load_settings()
    local = settings.local_url
    if args.launcher and port_in_use(settings.port):
        webbrowser.open(local)
        return
    storage = Storage(settings.data_dir)
    kroger = KrogerClient(settings.client_id, settings.client_secret, settings.redirect_uri, storage)
    backend = choose_backend(api_key=settings.anthropic_api_key)
    app = create_app(
        storage, kroger,
        photo_model=settings.photo_model, photo_timeout=settings.photo_timeout,
        analyzer=partial(analyze, api_key=settings.anthropic_api_key),
        photo_backend=backend, public_url=settings.public_url,
    )  # also migrates v1 data and ensures a default list
    if port_in_use(settings.port):
        sys.exit(
            f"Port {settings.port} is already in use (is Kroger Reorder already running?). "
            "Close it, or set PORT in .env and update the redirect URL on your Kroger app."
        )
    print(f"Kroger Reorder {__version__} running at {local}", flush=True)
    if settings.public_url != local:
        print(f"  Public URL:    {settings.public_url}", flush=True)
    if ip := lan_ip():
        print(f"  On your wifi:  http://{ip}:{settings.port}", flush=True)
    print(f"  Data folder:   {settings.data_dir}", flush=True)
    print(f"  Photos:        {PHOTO_LABELS[backend]}", flush=True)
    print("  Sign in to Kroger from this computer (or at the public URL); other devices share the sign-in.  (Ctrl+C to stop)",
          flush=True)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(local,)).start()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")
```

**Step 5: Run the suite and a smoke start**

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m kroger_reorder --version
```

Expected: all pass; `kroger-reorder 4.0.0`.

Then start the real server for 5 seconds to see the banner (it uses the live `.env` and `./data`, which is Jake's normal setup):

```bash
cd /c/Projects/kroger-reorder && (timeout 6 .venv/Scripts/python -m kroger_reorder --no-browser || true)
```

Expected banner lines include `Data folder:   C:\Projects\kroger-reorder\data` and `Photos:        Claude Code on this computer`.

**Step 6: Commit**

```bash
git add kroger_reorder/main.py kroger_reorder/__main__.py tests/test_routes.py
git commit -m "feat: report photo backend and public URL in /api/state; wire settings through the entry point

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Page shows the active photo backend and allows sign-in at the public URL

**Files:**
- Modify: `kroger_reorder/static/index.html:210` (photo dialog)
- Modify: `kroger_reorder/static/app.js:3`, `:6`, `:87-88`, `:139-142`, `:431-447`

No automated tests cover the page (vanilla JS, no harness). Verify in the browser at the end of the task.

**Step 1: `index.html`** — after the `<p class="muted">Snap your fridge…</p>` line inside `#photo-dialog`, add:

```html
  <p id="photo-backend" class="muted"></p>
```

**Step 2: `app.js`**

Line 3, replace the constant with a function that also accepts the configured public URL:

```js
const LOCAL_HOST = ['localhost', '127.0.0.1'].includes(location.hostname);
function isHost() { return LOCAL_HOST || (!!state.public_url && location.origin === state.public_url); }
```

Line 6, add the two fields to the initial state:

```js
let state = { store: null, signed_in: false, lists: [], active_list: null, name: '', items: [], warning: null,
              photo_backend: null, public_url: null };
```

Lines 87-88 become:

```js
  $('signin-link').classList.toggle('hidden', !isHost());
  $('signin-remote').classList.toggle('hidden', isHost());
```

In `refreshLists()` include the new keys:

```js
  Object.assign(state, { store: s.store, signed_in: s.signed_in, lists: s.lists, active_list: s.active_list,
                         photo_backend: s.photo_backend, public_url: s.public_url });
```

In `openPhoto()` after `banner(null, '', false, $('photo-banner'));` add:

```js
  $('photo-backend').textContent = {
    api: 'Photos are analyzed through the Anthropic API key in .env.',
    cli: 'Photos are analyzed by Claude Code on the computer running the app.',
  }[state.photo_backend] || 'Photo analysis isn\'t set up yet: add ANTHROPIC_API_KEY to .env, or install Claude Code on the computer running the app.';
```

**Step 3: Verify in the browser**

Start the server (`.venv/Scripts/python -m kroger_reorder`), open the Photo dialog, confirm the hint reads "Photos are analyzed by Claude Code on the computer running the app." Stop the server.

**Step 4: Commit**

```bash
git add kroger_reorder/static/index.html kroger_reorder/static/app.js
git commit -m "feat(ui): show the photo backend in the photo dialog; allow sign-in at PUBLIC_URL

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Docker

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`

**Step 1: `Dockerfile`**

```dockerfile
# Kroger Reorder in a container. Photos need ANTHROPIC_API_KEY (no Claude Code inside the image).
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY kroger_reorder ./kroger_reorder
RUN pip install --no-cache-dir .

ENV KROGER_REORDER_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

CMD ["kroger-reorder", "--no-browser"]
```

**Step 2: `.dockerignore`**

```
.git
.venv
.pytest_cache
__pycache__
*.egg-info
data
docs
img
scripts
tests
.env
.env.example
```

**Step 3: `docker-compose.yml`**

```yaml
# docker compose up -d      (reads .env for credentials; lists and tokens persist in ./data)
services:
  kroger-reorder:
    build: .
    env_file: .env
    ports:
      - "${PORT:-8000}:${PORT:-8000}"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

**Step 4: Build and run against a scratch data directory**

Do **not** use `docker compose up` here; it would mount Jake's real `./data`. Use a scratch dir and the real `.env` only for credentials:

```bash
cd /c/Projects/kroger-reorder
docker build -t kroger-reorder:test .
SCRATCH=$(mktemp -d "$TMPDIR/kroger-docker.XXXXXX" 2>/dev/null || mktemp -d)
docker run --rm -d --name kroger-test --env-file .env -e PUBLIC_URL=http://localhost:8765 -e PORT=8765 \
  -p 8765:8765 -v "$SCRATCH":/data kroger-reorder:test
sleep 4
docker logs kroger-test
curl -s http://localhost:8765/api/state
ls "$SCRATCH"
docker stop kroger-test
```

Expected: logs show `Kroger Reorder 4.0.0 running at http://localhost:8765`, `Data folder:   /data`, `Photos:        off (...)`; the state JSON has `"photo_backend": null`, `"public_url": "http://localhost:8765"`; the scratch dir contains `config.json` and `lists/`.

If `docker build` fails on `pip install .` because `license-files` or the `license` string is rejected, pin `setuptools>=77` is already in `build-system`; check the error text before changing anything.

**Step 5: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "feat: Dockerfile and docker-compose for self-hosting

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Lock file

**Files:**
- Create: `requirements.lock`

**Step 1: Generate**

```bash
cd /c/Projects/kroger-reorder
.venv/Scripts/python -m pip freeze --exclude-editable > requirements.lock
head -3 requirements.lock; grep -c . requirements.lock
```

Expected: ~25 lines, `anthropic==…` and `fastapi==…` present, no `-e` line.

Prepend a comment line:

```
# Exact versions the author tests with (Python 3.14, Windows). Regenerate with: pip freeze --exclude-editable
```

**Step 2: Prove it installs in a clean venv**

```bash
SCRATCH=$(mktemp -d)
python -m venv "$SCRATCH/venv"
"$SCRATCH/venv/Scripts/python" -m pip install -q -r requirements.lock
"$SCRATCH/venv/Scripts/python" -m pip install -q --no-deps .
cd "$SCRATCH" && KROGER_CLIENT_ID=x KROGER_CLIENT_SECRET=y "$SCRATCH/venv/Scripts/python" -m kroger_reorder --version
```

Expected: `kroger-reorder 4.0.0`. (`--version` exits before loading settings; the env vars are belt and braces.)

**Step 3: Commit**

```bash
git add requirements.lock
git commit -m "build: requirements.lock with exact tested versions

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -q

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t kroger-reorder .
```

**Step 2: Validate the YAML parses**

```bash
.venv/Scripts/python -c "import yaml" 2>/dev/null || .venv/Scripts/python -m pip install -q pyyaml
.venv/Scripts/python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"
```

Expected: `ok`. (pyyaml is a throwaway install in the dev venv, not a dependency.)

**Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: pytest on Windows, Ubuntu, macOS for Python 3.12 and 3.13; Docker build

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: README

**Files:**
- Modify: `README.md`

Make these edits in order. Keep everything not mentioned.

**Step 1: Badge and intro** — after the `# Kroger Reorder` heading, insert:

```markdown
[![CI](https://github.com/jrkubic/kroger-reorder/actions/workflows/ci.yml/badge.svg)](https://github.com/jrkubic/kroger-reorder/actions/workflows/ci.yml)
```

Change the second intro paragraph from `Runs on one Windows PC, is reachable from every phone on the same wifi, and stores everything as plain JSON files you can read and edit.` to:

```markdown
Runs on a Windows PC, a Mac, a Linux box, or in Docker; is reachable from every phone on the same wifi; and stores everything as plain JSON files you can read and edit.
```

**Step 2: Table of contents** — replace the `- [Running on startup]` entry with:

```markdown
- [Configuration](#configuration)
- [Running on startup (Windows)](#running-on-startup-windows)
- [Running on another machine](#running-on-another-machine)
```

**Step 3: Features** — replace the *Photo suggestions* bullet with:

```markdown
- **Photo suggestions.** Photograph your fridge, pantry or cupboard from any device on the wifi, add a note, and Claude proposes a list: what it sees, what looks low or missing, and what your note asks for. Every line is reviewed against the store catalog before it is added. Works through your own Anthropic API key, or through Claude Code installed on the host.
```

Replace `- **Start with Windows.** ...` with:

```markdown
- **Start with Windows.** One script installs a hidden startup entry so the app is always available. Docker users get the same with `restart: unless-stopped`.
```

**Step 4: Technology table** — change the Language row to `Python 3.12+ (tested on 3.12, 3.13, 3.14)`; change the Testing row's count after Task 11 reports it; replace the Vision row with:

```markdown
| Vision | [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) SDK, or [Claude Code](https://code.claude.com/docs) run headlessly (`claude -p`) | Photo analysis for suggested lists: API key, or the host's Claude subscription |
| Containers | Docker, docker compose | Optional self-hosting path |
| CI | GitHub Actions | pytest on Windows, Ubuntu, macOS × Python 3.12, 3.13; Docker build |
```

**Step 5: Project structure** — replace the block with:

```
kroger-reorder/
├── kroger_reorder/
│   ├── __init__.py        Package version
│   ├── __main__.py        Entry point: python -m kroger_reorder [--no-browser] [--launcher]
│   ├── main.py            FastAPI app factory and every HTTP route
│   ├── kroger.py          Kroger API client: OAuth, locations, products, cart
│   ├── storage.py         JSON persistence: lists, config, tokens, order history
│   ├── orders.py          Builds an order snapshot after a successful send
│   ├── importer.py        Parses text, Markdown, CSV, TSV, and JSON into lines
│   ├── vision.py          Photo suggestions: Anthropic API or headless Claude Code
│   ├── usage.py           Usage profile derived from order history
│   ├── settings.py        Reads configuration from .env
│   └── static/
│       ├── index.html     The single page and its styles
│       └── app.js         Page logic, talks to the JSON API
├── tests/                 pytest suite, one module per package module
├── docs/plans/            Design documents and implementation plans per version
├── scripts/
│   ├── install-startup.ps1     Adds or removes the Windows startup entry
│   └── install-start-menu.ps1  Adds or removes the Start Menu shortcut
├── .github/workflows/ci.yml   Tests on three OSes, Docker build
├── img/                   Screenshots for this README and the app icon
├── data/                  Your lists, config, tokens, orders (gitignored; see KROGER_REORDER_DATA)
├── .env                   Credentials and settings (gitignored)
├── .env.example           Template for .env, documents every setting
├── Dockerfile             Container image
├── docker-compose.yml     One-service compose file with a data volume
├── requirements.lock      Exact dependency versions the author tests with
└── pyproject.toml         Package metadata and dependencies
```

**Step 6: Getting started** — replace everything from `## Getting started` up to (not including) `## Using the app` with:

````markdown
## Getting started

### Prerequisites

- A Kroger account and a free developer app (step 1 below)
- One of: Python 3.12 or newer, or Docker

### 1. Register a Kroger developer app

1. Go to https://developer.kroger.com and sign in with your regular Kroger account.
2. Open **My Apps** and create a new app. Any name works, for example `kroger-reorder`.
3. Under **Redirect URL** enter exactly `http://localhost:8000/callback`. (Running the app on another machine? See [Running on another machine](#running-on-another-machine) for what to enter instead.)
4. Make sure the app can use the **Products**, **Locations**, and **Cart** APIs. If the portal does not ask, they are included.
5. Save, then copy the **Client ID** and **Client Secret**.

### 2. Get the code and fill in `.env`

```bash
git clone https://github.com/jrkubic/kroger-reorder.git
cd kroger-reorder
cp .env.example .env        # Windows PowerShell: copy .env.example .env
```

Open `.env` and paste in your Client ID and Client Secret. Every other setting is optional and documented in the file; see [Configuration](#configuration).

### 3. Install and run

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m kroger_reorder
```

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m kroger_reorder
```

**Docker**

```bash
docker compose up -d
docker compose logs -f     # Ctrl+C to stop following; the container keeps running
```

Lists, tokens, and order history persist in `./data`. Photo suggestions inside Docker need `ANTHROPIC_API_KEY` in `.env` (there is no Claude Code in the image).

Prefer the exact dependency versions the author tests with? `pip install -r requirements.lock` then `pip install --no-deps -e .`.

### 4. First run

Your browser opens http://localhost:8000 (Docker: open it yourself). Enter your zip code, pick your store, and click **Sign in to Kroger**. You do this once; the app stays signed in with a refresh token.

The console also prints a wifi address such as `http://192.168.1.20:8000`. Open that on your phone or any device on the same network. Sign in from the PC (or from the public URL, see below); every device shares the sign-in.

### 5. Optional: photo suggestions

The **Photo** button turns pictures of your fridge or pantry into a suggested list. Pick one of two backends:

| Backend | Set up | Cost |
|---|---|---|
| Anthropic API | Create a key at https://console.anthropic.com and put it in `.env` as `ANTHROPIC_API_KEY`. Works on every platform including Docker. | Pay per use. A four-photo request on the default model is a few cents. |
| Claude Code | Install [Claude Code](https://code.claude.com/docs) on the computer running the app, run `claude` once to sign in, and leave `ANTHROPIC_API_KEY` unset. | Uses your Claude subscription. Not available in Docker. |

The photo dialog says which backend is active. `PHOTO_MODEL` in `.env` applies to both; the default is `claude-opus-5-5`.

## Configuration

All settings live in `.env` (or the process environment, which wins). Only the first two are required.

| Variable | Default | What it does |
|---|---|---|
| `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET` | | From your Kroger developer app. |
| `PORT` | `8000` | Port to listen on. The redirect URL on the Kroger app must use the same port. |
| `PUBLIC_URL` | `http://localhost:<PORT>` | Origin browsers use to reach the app when it runs on another machine. The OAuth redirect becomes `<PUBLIC_URL>/callback`. |
| `KROGER_REORDER_DATA` | `./data` | Folder for lists, config, tokens, and order history. Docker sets it to `/data`. |
| `ANTHROPIC_API_KEY` | unset | Enables the API photo backend. Unset means Claude Code. |
| `PHOTO_MODEL` | `claude-opus-5-5` | Model used for photo suggestions by either backend. |
| `PHOTO_TIMEOUT` | `90` | Seconds to wait for a photo analysis. |

The server prints the data folder and the active photo backend when it starts, so you can confirm what it picked up.

## Running on startup (Windows)

To keep the server running whenever the PC is signed in, so your phone can open the list without touching the computer:

```powershell
cd kroger-reorder
.\scripts\install-startup.ps1
```

This adds a "Kroger Reorder" entry to your Startup folder that runs the server hidden, with no window and no browser pop-up. It starts at your next sign-in; double-click the shortcut in the Startup folder to start it now. Output goes to `%LOCALAPPDATA%\kroger-reorder\server.log`.

Remove it with `.\scripts\install-startup.ps1 -Remove`, or toggle it under Settings > Apps > Startup.

Check two things once:

- The first hidden start may trigger a Windows Firewall prompt for `pythonw.exe` on private networks. Click **Allow**, or phones cannot reach it.
- The wifi address comes from your router and can change. Reserve the PC's address in the router's DHCP settings so the phone bookmark keeps working.

`python -m kroger_reorder --no-browser` is the same thing run by hand without opening a browser tab.

### Start Menu shortcut

For a one-click way in, add a "Kroger Reorder" app to the Start Menu:

```powershell
.\scripts\install-start-menu.ps1
```

Press the Windows key, type "Kroger", and press Enter. If the server is already running it just opens the page; if not, it starts the server hidden and then opens the page. The icon is `img/kroger-reorder.ico`. Remove it with `.\scripts\install-start-menu.ps1 -Remove`. Under the hood the shortcut runs `python -m kroger_reorder --launcher`.

On macOS or Linux, use a user-level launchd plist or systemd unit that runs `.venv/bin/kroger-reorder --no-browser` from the checkout directory, or just use Docker with `restart: unless-stopped`.

## Running on another machine

The Kroger sign-in is a browser redirect back to the app, so the browser you sign in from has to reach the app at a URL that is registered on your Kroger developer app. Two ways when the app runs on a NAS, a Raspberry Pi, or a server:

**Set `PUBLIC_URL`.** Put `PUBLIC_URL=http://nas.local:8000` (whatever address you use to reach the box) in `.env`, and register `http://nas.local:8000/callback` as the redirect URL on the Kroger app. The **Sign in to Kroger** button then appears when you open the app at that exact address. Kroger's portal accepts plain `http://` for private addresses at the time of writing; if it insists on `https://`, put the app behind any reverse proxy that terminates TLS and use that URL.

**Or tunnel once.** Leave `PUBLIC_URL` unset, run `ssh -L 8000:localhost:8000 user@nas` from a PC, open http://localhost:8000 there, and sign in. Tokens are stored on the server, so this is a one-time step until Kroger revokes the refresh token.

Phones on the wifi open the app by its normal address either way; they never need to sign in.
````

**Step 7: Development** — replace the two-line install/test block with:

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"     # macOS/Linux: .venv/bin/python
.venv\Scripts\python -m pytest -q
```

Add a bullet to Conventions:

```markdown
- CI runs the suite on Windows, Ubuntu, and macOS for Python 3.12 and 3.13, and builds the Docker image, on every push and pull request.
- Bump `__version__` in `kroger_reorder/__init__.py` and add a row to [Version history](#version-history) when a version ships.
```

**Step 8: Limitations** — replace the last bullet (`Sign-in works only from the PC running the app…`) with:

```markdown
- Sign-in works from the machine running the app, or from the address in `PUBLIC_URL`. Other devices share the stored tokens.
```

**Step 9: Troubleshooting** — replace the two Claude Code rows and add API rows:

```markdown
| `Photos can't be analyzed: set ANTHROPIC_API_KEY...` after **Suggest a list** | Neither backend is configured. Add `ANTHROPIC_API_KEY` to `.env`, or install Claude Code on the computer running the app and sign in, then restart the app. |
| `Anthropic rejected ANTHROPIC_API_KEY` | The key is wrong, revoked, or has no credit. Check it at https://console.anthropic.com. |
| `Anthropic rate limit reached` | Wait a minute. Persistent? Your Anthropic plan's limits are lower than four photos per request allows; try fewer photos. |
| `Claude Code failed: ...` | Usually a sign-in or usage-limit problem on the PC. Run `claude` there once to check, then try again. |
| Sign-in button missing on a NAS or server install | Open the app at exactly the address in `PUBLIC_URL`, or use the SSH tunnel in [Running on another machine](#running-on-another-machine). |
| Docker: `Kroger sign-in failed` with a redirect mismatch | The redirect URL on the Kroger app must be `<PUBLIC_URL>/callback`; set `PUBLIC_URL` in `.env` to the address you open in the browser. |
```

**Step 10: Version history** — add:

```markdown
| 2026-10-28 | v5 | Package renamed to `kroger_reorder`, Anthropic API photo backend, `PUBLIC_URL` and `KROGER_REORDER_DATA`, Docker, CI on three OSes, lock file, clone-first setup docs |
```

**Step 11: Test count** — run `.venv/Scripts/python -m pytest -q | tail -1` and put the number in the Technology table's Testing row.

**Step 12: Check for stale references**

```bash
grep -n "python -m app\|app/\|claude-opus-5\b" README.md
```

Expected: no matches except `app.js`/`app/main.py` phrases are gone and `img/` paths remain.

**Step 13: Commit**

```bash
git add README.md
git commit -m "docs: clone-first setup for Windows, macOS, Linux, Docker; configuration table; photo backends; remote sign-in

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Windows shortcuts still work after the rename, then push

**Step 1: Re-run the shortcut installers** so the `.lnk` files point at `-m kroger_reorder`

```powershell
cd C:\Projects\kroger-reorder
.\scripts\install-start-menu.ps1
.\scripts\install-startup.ps1
```

Expected: both print `Installed ...`. If a server from the old `app` module is still running from startup, stop it first (Task Manager, `pythonw.exe`) so the port test in Step 2 is meaningful.

**Step 2: Launch via the Start Menu shortcut** and confirm the page opens and the Photo dialog shows the backend hint. Close the hidden server afterwards if it was not running before (`Stop-Process -Name pythonw`).

**Step 3: Full suite one more time, then push**

```bash
cd /c/Projects/kroger-reorder
.venv/Scripts/python -m pytest -q
git status --short
git log --oneline origin/main..main
git push origin main
```

Expected: all pass, clean tree, 15-ish commits pushed. Open https://github.com/jrkubic/kroger-reorder/actions and confirm the CI run goes green on all seven jobs. If a matrix job fails, read the log, fix, and push again; the Windows job is the one most likely to differ from local (path separators in tests are already handled by `tmp_path`).

**Step 4: Update memory**

Edit `C:\Users\Jake\.claude\projects\c--Jake-Old-PC-Docs-Resumes\memory\project-kroger-reorder.md`: v5 shipped on 2026-10-28, package is `kroger_reorder`, run with `python -m kroger_reorder`, photo backend selection, Docker and CI present. Update the index line in `MEMORY.md`.
