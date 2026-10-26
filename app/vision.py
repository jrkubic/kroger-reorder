# app/vision.py
"""Turn photos of a fridge, pantry or cupboard into grocery suggestions via headless Claude Code.

Jake chose to run this on his Claude subscription, so instead of the Anthropic API the server shells out
to the installed `claude` CLI (`claude -p … --output-format json`) and lets it read the photos with its
Read tool. The subprocess runner is injectable so tests never start Claude Code.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_ITEMS = 40
MAX_QUANTITY = 10
KINDS = ("seen", "suggested")

SYSTEM = """You are helping build a grocery shopping list from photos of a household's fridge, freezer, pantry or cupboard, together with the user's note and their purchase history.

Reply with ONLY a JSON array, no prose and no code fence. Each element:
{"name": "<generic product name as you'd type into a grocery store search>", "quantity": <integer 1-10>, "kind": "seen" or "suggested", "reason": "<one short sentence>"}

Include:
- items visible in the photos that a household would re-buy ("seen");
- items that look empty, nearly empty, or are clearly absent but implied by the photos, the note, or the purchase history ("suggested"), for example staples bought regularly that are overdue;
- anything the note asks for.

Prefer plain names like "whole milk" or "large eggs" over brands unless a brand is clearly visible. Skip non-food household items unless visible or asked for. At most 40 items. Do not repeat items."""


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


Runner = Callable[[list[str], float], RunResult]


def run_cli(args: list[str], timeout: float) -> RunResult:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VisionError(504, f"Claude took longer than {int(timeout)} seconds; try fewer or smaller photos.") from exc
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def build_prompt(photos: list[Path], note: str, profile: str) -> str:
    lines = ["Use the Read tool to look at each of these photos:"]
    lines += [f"- {p}" for p in photos]
    lines.append("")
    lines.append("User's note: " + (note.strip() or "(none)"))
    lines.append("")
    lines.append("Purchase history (most bought first):")
    lines.append(profile.strip() or "(no orders yet)")
    lines.append("")
    lines.append(SYSTEM)
    return "\n".join(lines)


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


def analyze(photos: list[Path], note: str, profile: str, *, model: str, timeout: float,
            runner: Runner = run_cli, which: Callable[[str], str | None] = shutil.which) -> list[Suggestion]:
    exe = which("claude")
    if not exe:
        raise VisionError(503, "Claude Code isn't installed on the computer running the app, so photos can't be analyzed.")
    args = [exe, "-p", build_prompt(photos, note, profile), "--output-format", "json",
            "--allowedTools", "Read", "--max-turns", "6"]
    if model:
        args += ["--model", model]
    result = runner(args, timeout)
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
