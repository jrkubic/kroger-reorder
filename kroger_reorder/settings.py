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
