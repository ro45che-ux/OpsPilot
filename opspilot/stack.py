"""Run the testbed as local processes (no Docker needed). Mirrors docker-compose.yml."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalStack:
    def __init__(self, state: Path, logs_dir: Path, rate: float = 40.0, seed: int = 1):
        self.state = Path(state)
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.rate, self.seed = rate, seed
        self.api_port, self.worker_port = free_port(), free_port()
        self.procs: dict[str, subprocess.Popen] = {}
        self.env = {**os.environ, "OPSPILOT_STATE_DIR": str(self.state), "PYTHONUNBUFFERED": "1",
                    "WORKER_METRICS_PORT": str(self.worker_port)}

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def metrics_targets(self) -> dict[str, str]:
        return {"api": f"{self.api_url}/metrics", "worker": f"http://127.0.0.1:{self.worker_port}/metrics"}

    def _spawn(self, name: str, args: list[str]) -> None:
        out = open(self.logs_dir / f"{name}.out", "ab")
        self.procs[name] = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=self.env, stdout=out,
                                            stderr=subprocess.STDOUT, start_new_session=True)

    def _wait_http(self, url: str, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"{url} not ready after {timeout}s; see {self.logs_dir}")

    def start_service(self, name: str) -> None:
        if name == "api":
            self._spawn("api", ["-m", "uvicorn", "testbed.api.app:app", "--host", "127.0.0.1",
                                "--port", str(self.api_port), "--log-level", "warning", "--no-access-log"])
            self._wait_http(f"{self.api_url}/health")
        elif name == "worker":
            self._spawn("worker", ["-m", "testbed.worker.main"])
            self._wait_http(self.metrics_targets["worker"])
        else:
            raise ValueError(name)

    def start(self) -> None:
        self.start_service("api")
        self.start_service("worker")
        self._spawn("loadgen", ["-m", "testbed.loadgen.loadgen", "--url", self.api_url,
                                "--rate", str(self.rate), "--seed", str(self.seed)])

    def stop_service(self, name: str) -> None:
        p = self.procs.pop(name, None)
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)

    def restart(self, name: str) -> None:
        self.stop_service(name)
        self.start_service(name)

    def stop(self) -> None:
        for name in ("loadgen", "api", "worker"):
            self.stop_service(name)
