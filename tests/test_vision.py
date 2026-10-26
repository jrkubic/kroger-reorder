# tests/test_vision.py
import json
from pathlib import Path

import pytest

from app.vision import RunResult, VisionError, analyze, build_prompt, parse_suggestions

PHOTOS = [Path("C:/tmp/a.jpg"), Path("C:/tmp/b.jpg")]
GOOD = [{"name": "whole milk", "quantity": 1, "kind": "seen", "reason": "Half a gallon left"},
        {"name": "large eggs", "quantity": "2", "kind": "suggested", "reason": "Carton is empty"}]


def envelope(result, **over):
    body = {"type": "result", "subtype": "success", "is_error": False, "result": result}
    body.update(over)
    return json.dumps(body)


def fake_runner(returncode=0, stdout="", stderr=""):
    calls = []

    def run(args, stdin_text, timeout):
        calls.append((args, stdin_text, timeout))
        return RunResult(returncode, stdout, stderr)
    run.calls = calls
    return run


def which_ok(name):
    return r"C:\tools\claude.cmd"


# -- prompt -------------------------------------------------------------------------

def test_prompt_mentions_photos_note_and_history():
    text = build_prompt(PHOTOS, "out of eggs", "- Whole Milk: bought 3x")
    assert "C:\\tmp\\a.jpg" in text or "C:/tmp/a.jpg" in text
    assert "User's note: out of eggs" in text
    assert "- Whole Milk: bought 3x" in text
    assert "JSON array" in text


def test_prompt_placeholders_when_empty():
    text = build_prompt(PHOTOS, "  ", "")
    assert "User's note: (none)" in text and "(no orders yet)" in text


# -- parsing ------------------------------------------------------------------------

def test_parse_good_output_clamps_and_tags():
    out = parse_suggestions(json.dumps(GOOD))
    assert [(s.name, s.quantity, s.kind, s.reason) for s in out] == [
        ("whole milk", 1, "seen", "Half a gallon left"), ("large eggs", 2, "suggested", "Carton is empty")]


def test_parse_tolerates_fences_prose_bad_kinds_and_duplicates():
    text = 'Here you go:\n```json\n[{"name":"Milk","quantity":99,"kind":"weird"},{"name":"milk"},{"name":""},5]\n```'
    out = parse_suggestions(text)
    assert [(s.name, s.quantity, s.kind) for s in out] == [("Milk", 10, "seen")]


def test_parse_caps_item_count():
    out = parse_suggestions(json.dumps([{"name": f"item {i}"} for i in range(60)]))
    assert len(out) == 40


def test_parse_garbage_is_502():
    with pytest.raises(VisionError) as exc:
        parse_suggestions("I can't see anything.")
    assert exc.value.status == 502


# -- analyze --------------------------------------------------------------------------

def test_analyze_runs_cli_with_photos_and_parses_result():
    run = fake_runner(stdout=envelope(json.dumps(GOOD)))
    out = analyze(PHOTOS, "note", "history", model="claude-opus-5", timeout=42, runner=run, which=which_ok)
    assert [s.name for s in out] == ["whole milk", "large eggs"]
    (args, stdin_text, timeout), = run.calls
    assert args[0] == r"C:\tools\claude.cmd" and args[1] == "-p"
    assert "--output-format" in args and "json" in args
    assert args[args.index("--allowedTools") + 1] == "Read"
    assert args[args.index("--model") + 1] == "claude-opus-5"
    # The prompt goes on stdin, never as an argument: the Windows .CMD shim truncates multi-line args.
    assert not any("\n" in a for a in args)
    assert "note" in stdin_text and "history" in stdin_text and "a.jpg" in stdin_text
    assert timeout == 42


def test_analyze_missing_cli_is_503():
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=fake_runner(), which=lambda n: None)
    assert exc.value.status == 503


def test_analyze_nonzero_exit_reports_last_stderr_line():
    run = fake_runner(returncode=1, stderr="warning\nNot logged in. Please run /login")
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=run, which=which_ok)
    assert exc.value.status == 502 and "Not logged in" in exc.value.message


def test_analyze_error_envelope_is_502():
    run = fake_runner(stdout=envelope("rate limited", is_error=True, subtype="error_during_execution"))
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=run, which=which_ok)
    assert exc.value.status == 502 and "rate limited" in exc.value.message


def test_analyze_non_json_stdout_is_502():
    run = fake_runner(stdout="<html>oops")
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=run, which=which_ok)
    assert exc.value.status == 502


def test_analyze_timeout_bubbles_as_504():
    def slow(args, stdin_text, timeout):
        raise VisionError(504, "too slow")
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=slow, which=which_ok)
    assert exc.value.status == 504
