"""Plain-JSON persistence for the standard list, store config, and Kroger tokens."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
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
        self._lock = threading.RLock()  # re-entrant: save_list -> load_list -> _read

    # -- generic helpers ----------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _read(self, name: str) -> Any | None:
        """Parsed JSON, or None if the file is missing. Raises ValueError if corrupt."""
        path = self._path(name)
        with self._lock:  # on Windows, replace() fails while another thread has the file open
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} is not valid JSON: {exc}") from exc

    def _write(self, name: str, value: Any) -> None:
        path = self._path(name)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
            tmp.replace(path)

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
        try:
            items = [
                Item(
                    upc=str(e["upc"]),
                    name=str(e.get("name", "")),
                    size=str(e.get("size", "")),
                    quantity=int(e.get("quantity", 1)),
                    included=bool(e.get("included", True)),
                )
                for e in raw
            ]
        except (TypeError, ValueError, KeyError) as exc:
            return ListLoad(
                items=[],
                warning=f"list.json has an entry with unexpected or missing fields ({exc}). "
                "Showing an empty list; the file is untouched until you add an item.",
            )
        return ListLoad(items=items)

    def save_list(self, items: list[Item]) -> None:
        with self._lock:
            # Any load warning means the file holds something we could not fully read;
            # move it aside rather than overwrite it.
            if self.load_list().warning and self._path("list.json").exists():
                self._backup("list.json")
            self._write("list.json", [asdict(i) for i in items])

    def _backup(self, name: str) -> None:
        """Rename <name> to <name>.corrupt-<timestamp>."""
        path = self._path(name)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path.replace(path.with_name(f"{name}.corrupt-{stamp}"))

    # -- store config ------------------------------------------------------
    def load_config(self) -> dict:
        try:
            raw = self._read("config.json")
        except ValueError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def save_config(self, config: dict) -> None:
        self._write("config.json", config)

    # -- OAuth tokens ------------------------------------------------------
    def load_tokens(self) -> dict | None:
        try:
            raw = self._read("tokens.json")
        except ValueError:
            return None
        return raw if isinstance(raw, dict) else None

    def save_tokens(self, tokens: dict) -> None:
        self._write("tokens.json", tokens)

    def clear_tokens(self) -> None:
        self._path("tokens.json").unlink(missing_ok=True)
