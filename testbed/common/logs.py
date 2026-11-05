"""Structured JSON logging. One line per event, written to stdout and state/logs/<service>.jsonl."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable


class JsonLogger:
    def __init__(self, service: str, version_fn: Callable[[], str], log_dir: Path):
        self.service = service
        self._version_fn = version_fn
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self._file = open(log_dir / f"{service}.jsonl", "a", buffering=1)
        self._lock = threading.Lock()

    def log(self, level: str, msg: str, **fields) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "level": level,
            "service": self.service,
            "version": self._version_fn(),
            "msg": msg,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = json.dumps(rec, default=str)
        with self._lock:
            self._file.write(line + "\n")
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def info(self, msg: str, **f) -> None:
        self.log("INFO", msg, **f)

    def warning(self, msg: str, **f) -> None:
        self.log("WARNING", msg, **f)

    def error(self, msg: str, **f) -> None:
        self.log("ERROR", msg, **f)
