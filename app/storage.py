"""Plain-JSON persistence for the standard list, store config, and Kroger tokens."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Item:
    upc: str
    name: str
    size: str
    quantity: int = 1
    included: bool = True


@dataclass
class ListLoad:
    items: list[Item]
    warning: str | None = None


class Storage:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- generic helpers ----------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _read(self, name: str) -> Any | None:
        """Parsed JSON, or None if the file is missing. Raises ValueError if corrupt."""
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} is not valid JSON: {exc}") from exc

    def _write(self, name: str, value: Any) -> None:
        self._path(name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    # -- standard list -----------------------------------------------------
    def load_list(self) -> ListLoad:
        try:
            raw = self._read("list.json")
        except ValueError as exc:
            return ListLoad(
                items=[],
                warning=f"{exc}. Showing an empty list; the file is untouched until you add an item.",
            )
        if raw is None:
            return ListLoad(items=[])
        if not isinstance(raw, list):
            return ListLoad(items=[], warning="list.json should contain a JSON array. Showing an empty list.")
        return ListLoad(items=[Item(**entry) for entry in raw])

    def save_list(self, items: list[Item]) -> None:
        self._write("list.json", [asdict(i) for i in items])

    # -- store config ------------------------------------------------------
    def load_config(self) -> dict:
        try:
            return self._read("config.json") or {}
        except ValueError:
            return {}

    def save_config(self, config: dict) -> None:
        self._write("config.json", config)

    # -- OAuth tokens ------------------------------------------------------
    def load_tokens(self) -> dict | None:
        try:
            return self._read("tokens.json")
        except ValueError:
            return None

    def save_tokens(self, tokens: dict) -> None:
        self._write("tokens.json", tokens)

    def clear_tokens(self) -> None:
        self._path("tokens.json").unlink(missing_ok=True)
