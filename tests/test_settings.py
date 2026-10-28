from pathlib import Path

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
    assert s.photo_model == "claude-opus-5-5" and s.photo_timeout == 90.0
    s = load_settings(env={**base, "PHOTO_MODEL": " claude-sonnet-5 ", "PHOTO_TIMEOUT": "30"})
    assert s.photo_model == "claude-sonnet-5" and s.photo_timeout == 30.0
    with pytest.raises(SystemExit) as info:
        load_settings(env={**base, "PHOTO_TIMEOUT": "soon"})
    assert "PHOTO_TIMEOUT" in str(info.value)


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
