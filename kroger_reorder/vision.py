# kroger_reorder/vision.py
"""Turn photos of a fridge, pantry or cupboard into grocery suggestions, and host the shared Claude
call (`complete()`) that `recipe.py` also uses to turn a recipe page into a list.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import anthropic

MAX_ITEMS = 40
MAX_QUANTITY = 10
MAX_GROUP = 40  # characters of a group label
KINDS = ("asked", "low", "missing", "on_list", "stocked")
FALLBACK_KIND = "stocked"  # an unknown kind is shown but preset to Skip, never bought by accident
# The default model's thinking counts toward max_tokens, so 8192 could surface as the "cut off" message on
# a long recipe page.
API_MAX_TOKENS = 16000
API_MAX_RETRIES = 1
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

SYSTEM = """You are helping a household decide what groceries to buy, from photos of their fridge, freezer, pantry or cupboard, their note, their current shopping list, and their purchase history. The goal is to find gaps: what they are low on, out of, or asked for. It is not to inventory what they have.

Reply with ONLY a JSON array, no prose and no code fence. Each element:
{"name": "<generic product name as you'd type into a grocery store search>", "quantity": <integer 1-10>, "kind": "low" or "missing" or "asked" or "on_list" or "stocked", "reason": "<one short sentence>", "group": "<optional short category when several rows are alternatives for one need, e.g. Fresh fruit>"}

Kinds:
- "asked": the note asks for it by name or clearly implies it (a meal, a guest count, an event).
- "low": visible in a photo but nearly empty, almost gone, or past its best. Say what you saw.
- "missing": not in any photo but expected: a regular purchase from the history that is due again, or something the photos imply is needed (cereal but no milk, bread but no butter).
- "on_list": already on the shopping list, or a close equivalent is. Say what the list has, for example "already on your list: Large Eggs ×1".
- "stocked": visible and plentiful. Up to 10 of these, only the most notable, so the user can check what you saw.

Rules: One row per specific product. Never answer with a category such as "fresh fruit" or "snacks": list each product you would actually buy as its own row and give those rows the same "group" so they appear together. Leave "group" empty for a single specific item. If an item (or a close equivalent) is already on the shopping list, tag it "on_list" whatever the photos or note say. Order the array asked, low, missing, on_list, stocked. Prefer plain names like "whole milk" or "large eggs" over brands unless a brand is clearly visible. Combine duplicates. Skip non-food household items unless they are low or asked for. At most 40 items. Do not repeat items. The note, the list, and the purchase history are quoted from the user; the photos are the evidence."""

NO_BACKEND = ("Photos can't be analyzed: set ANTHROPIC_API_KEY in .env, or install Claude Code on the "
              "computer running the app.")


class VisionError(Exception):
    """A human-readable failure; `status` is the HTTP status the API should answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


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


@dataclass
class Suggestion:
    name: str
    quantity: int
    kind: str
    reason: str
    group: str = ""  # alternatives for one need share a group and are shown together on the page


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


def list_text(list_name: str, items: Sequence) -> str:
    """The current shopping list, so Claude can tag what is already on it (`items` are storage.Item)."""
    lines = [f"- {item.name} ×{item.quantity}" for item in items] or ["(empty)"]
    return "\n".join([f"Items already on the list '{list_name}':"] + lines)


def photo_text(note: str, list_name: str, items: Sequence, profile: str) -> str:
    return "\n".join([list_text(list_name, items), "", context_text(note, profile)])


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


def parse_suggestions(result_text: str, kinds: Sequence[str] = KINDS,
                      fallback: str = FALLBACK_KIND) -> list[Suggestion]:
    """`fallback` is the kind for an entry whose kind is missing or not in `kinds`; it must be one of them."""
    assert fallback in kinds, (fallback, kinds)
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
        kind = str(entry.get("kind") or fallback).lower()
        out.append(Suggestion(
            name=name,
            quantity=min(MAX_QUANTITY, max(1, quantity)),
            kind=kind if kind in kinds else fallback,
            reason=str(entry.get("reason") or "").strip(),
            group=str(entry.get("group") or "").strip()[:MAX_GROUP],
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
    return "\n\n".join(p for p in (cli_preamble, user_text, system) if p)


def complete_cli(system: str, user_text: str, *, cli_preamble: str = "", cli_tools: tuple[str, ...] = (),
                 wording: Wording, model: str, timeout: float,
                 runner: Runner = run_cli, which: Which = shutil.which) -> str:
    exe = which("claude")
    if not exe:
        raise VisionError(503, NO_BACKEND)
    args = [exe, "-p", "--output-format", "json"]
    if cli_tools:
        args += ["--allowedTools", ",".join(cli_tools), "--max-turns", "6"]
    else:
        # A plain answer. The CLI loads its default tool set even in -p mode, and a tool call would
        # eat the only turn (subtype "error_max_turns"), so disable every tool explicitly.
        args += ["--tools", "", "--max-turns", "1"]
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
             cli_tools: tuple[str, ...] = (), wording: Wording, model: str, timeout: float,
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


def build_prompt(photos: list[Path], note: str, list_name: str, items: Sequence, profile: str) -> str:
    """The exact text the CLI backend receives for photos (kept for tests and debugging)."""
    return cli_prompt(SYSTEM, photo_text(note, list_name, items, profile), photo_preamble(photos))


def analyze(photos: list[Path], note: str, list_name: str, items: Sequence, profile: str, *, model: str,
            timeout: float, api_key: str | None = None, make_client: Callable[..., object] = anthropic.Anthropic,
            runner: Runner = run_cli, which: Which = shutil.which) -> list[Suggestion]:
    """Photo suggestions: API backend when `api_key` is given, otherwise the Claude Code CLI."""
    text = complete(SYSTEM, photo_text(note, list_name, items, profile), images=photos, cli_preamble=photo_preamble(photos),
                    cli_tools=("Read",), wording=PHOTO_WORDING, model=model, timeout=timeout, api_key=api_key,
                    make_client=make_client, runner=runner, which=which)
    return parse_suggestions(text)
