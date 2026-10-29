# tests/test_vision.py
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from kroger_reorder.vision import (SYSTEM, CliTimeout, RunResult, VisionError, Wording, analyze, build_prompt,
                                   choose_backend, complete, context_text, parse_suggestions)

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
        raise CliTimeout()
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=slow, which=which_ok)
    assert exc.value.status == 504 and "fewer or smaller photos" in exc.value.message


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
    (api_error(anthropic.NotFoundError, 404), 502, "PHOTO_MODEL"),
    (api_error(anthropic.OverloadedError, 529), 503, "busy"),
    (api_error(anthropic.ServiceUnavailableError, 503), 503, "busy"),
    (api_error(anthropic.BadRequestError, 400, "Error code: 400"), 502, "Error code: 400"),
    (anthropic.APIError("response validation broke", httpx2.Request("POST", "https://x"), body=None), 502,
     "validation broke"),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), 504,
     "longer than 42 seconds; try fewer or smaller photos"),
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


def test_analyze_api_max_tokens_is_502(jpg_photos):
    make = fake_client(result=api_message(json.dumps(GOOD), stop_reason="max_tokens"))
    with pytest.raises(VisionError) as exc:
        analyze(jpg_photos, "", "", model="m", timeout=5, api_key="sk", make_client=make)
    assert exc.value.status == 502 and "cut off" in exc.value.message


def test_analyze_api_bad_request_surfaces_inner_api_message(jpg_photos):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError(
        "Error code: 400 - {...}", response=httpx2.Response(400, request=request),
        body={"type": "error", "error": {"type": "invalid_request_error", "message": "image exceeds 5 MB maximum"}})
    with pytest.raises(VisionError) as exc:
        analyze(jpg_photos, "", "", model="m", timeout=5, api_key="sk", make_client=fake_client(error=error))
    assert exc.value.status == 502 and "image exceeds 5 MB maximum" in exc.value.message


def test_analyze_api_tolerates_non_dict_error_body(jpg_photos):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError("Error code: 400", response=httpx2.Response(400, request=request),
                                      body={"error": "unauthorized"})
    with pytest.raises(VisionError) as exc:
        analyze(jpg_photos, "", "", model="m", timeout=5, api_key="sk", make_client=fake_client(error=error))
    assert exc.value.status == 502 and "Error code: 400" in exc.value.message


def test_analyze_without_key_or_cli_explains_both_options():
    with pytest.raises(VisionError) as exc:
        analyze(PHOTOS, "", "", model="m", timeout=5, runner=fake_runner(), which=lambda n: None)
    assert exc.value.status == 503
    assert "ANTHROPIC_API_KEY" in exc.value.message and "Claude Code" in exc.value.message


# -- generic complete() -----------------------------------------------------------------

WORDING = Wording(timeout="try a shorter page", refusal="Claude declined; try another page.",
                  truncated="Claude's answer was cut off; try a shorter page.")


def test_complete_cli_without_tools_passes_no_allowed_tools_and_one_turn():
    run = fake_runner(stdout=envelope("[]"))
    text = complete("SYS", "USER", wording=WORDING, model="m", timeout=5, runner=run, which=which_ok)
    assert text == "[]"
    (args, stdin_text, timeout), = run.calls
    assert "--allowedTools" not in args
    assert args[args.index("--tools") + 1] == ""
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


@pytest.mark.parametrize("stop, expected", [
    ("refusal", "Claude declined; try another page."),
    ("max_tokens", "Claude's answer was cut off; try a shorter page."),
])
def test_complete_api_refusal_and_truncation_use_callers_wording(stop, expected):
    make = fake_client(result=api_message("", stop_reason=stop))
    with pytest.raises(VisionError) as exc:
        complete("SYS", "USER", wording=WORDING, model="m", timeout=5, api_key="k", make_client=make)
    assert exc.value.status == 502 and exc.value.message == expected


def test_parse_suggestions_accepts_custom_kinds():
    out = parse_suggestions(json.dumps([{"name": "flour", "kind": "staple"}, {"name": "milk", "kind": "seen"}]),
                            kinds=("needed", "staple", "on_list"))
    assert [(s.name, s.kind) for s in out] == [("flour", "staple"), ("milk", "needed")]
