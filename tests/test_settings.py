import pytest

from kroger_reorder.settings import load_settings


def test_missing_credentials_exit_with_helpful_message():
    with pytest.raises(SystemExit) as info:
        load_settings(env={})
    msg = str(info.value)
    assert "KROGER_CLIENT_ID" in msg
    assert "KROGER_CLIENT_SECRET" in msg
    assert "developer.kroger.com" in msg


def test_only_missing_names_are_reported():
    with pytest.raises(SystemExit) as info:
        load_settings(env={"KROGER_CLIENT_ID": "cid"})
    assert "KROGER_CLIENT_ID" not in str(info.value)
    assert "KROGER_CLIENT_SECRET" in str(info.value)


def test_defaults_and_redirect_uri():
    s = load_settings(env={"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec"})
    assert s.client_id == "cid"
    assert s.client_secret == "sec"
    assert s.port == 8000
    assert s.redirect_uri == "http://localhost:8000/callback"


def test_custom_port_changes_redirect_uri():
    s = load_settings(env={"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec", "PORT": "9100"})
    assert s.port == 9100
    assert s.redirect_uri == "http://localhost:9100/callback"


def test_non_numeric_port_exits_with_message():
    with pytest.raises(SystemExit) as info:
        load_settings(env={"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec", "PORT": "abc"})
    assert str(info.value) == "PORT must be a number, got 'abc'."


def test_photo_defaults_and_overrides():
    base = {"KROGER_CLIENT_ID": "cid", "KROGER_CLIENT_SECRET": "sec"}
    s = load_settings(env=base)
    assert s.photo_model == "claude-opus-5" and s.photo_timeout == 90.0
    s = load_settings(env={**base, "PHOTO_MODEL": " claude-sonnet-5 ", "PHOTO_TIMEOUT": "30"})
    assert s.photo_model == "claude-sonnet-5" and s.photo_timeout == 30.0
    with pytest.raises(SystemExit) as info:
        load_settings(env={**base, "PHOTO_TIMEOUT": "soon"})
    assert "PHOTO_TIMEOUT" in str(info.value)
