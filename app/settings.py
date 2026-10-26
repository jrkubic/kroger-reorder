"""Reads Kroger credentials from .env / environment and refuses to start without them."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv

REQUIRED = ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET")


DEFAULT_PHOTO_MODEL = "claude-opus-5"
DEFAULT_PHOTO_TIMEOUT = 90.0


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    port: int
    photo_model: str = DEFAULT_PHOTO_MODEL   # model Claude Code uses for photo suggestions
    photo_timeout: float = DEFAULT_PHOTO_TIMEOUT  # seconds before a photo analysis is abandoned

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/callback"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env`, or from .env + os.environ when `env` is None."""
    if env is None:
        load_dotenv()
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
    raw_timeout = env.get("PHOTO_TIMEOUT") or str(DEFAULT_PHOTO_TIMEOUT)
    try:
        photo_timeout = float(raw_timeout)
    except ValueError:
        sys.exit(f"PHOTO_TIMEOUT must be a number of seconds, got '{raw_timeout}'.")
    return Settings(
        client_id=env["KROGER_CLIENT_ID"],
        client_secret=env["KROGER_CLIENT_SECRET"],
        port=port,
        photo_model=(env.get("PHOTO_MODEL") or DEFAULT_PHOTO_MODEL).strip(),
        photo_timeout=photo_timeout,
    )
