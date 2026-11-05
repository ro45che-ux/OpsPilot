"""Shared on-disk state for the testbed: release manifest, faults, change history, logs, db."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    p = Path(os.environ.get("OPSPILOT_STATE_DIR", "state")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic replace so readers never see a half-written file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


class JsonFileView:
    """Re-reads a JSON file when its mtime changes, so a 'deploy' takes effect without a restart."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: int | None = -1
        self._data: Any = None
        self.generation = 0

    def get(self) -> Any:
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if m != self._mtime:
            self._mtime = m
            self._data = read_json(self.path) if m is not None else None
            self.generation += 1
        return self._data
