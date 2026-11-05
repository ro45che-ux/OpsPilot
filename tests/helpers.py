"""Tiny synthetic world for unit tests: no snapshots, no processes."""
from __future__ import annotations

from opspilot import telemetry as T
from opspilot.env import Env
from opspilot.schemas import Action, Alert, Change


def change(cid="chg-x", ts=80.0, service="api", ctype="deploy", **kw) -> Change:
    base = dict(id=cid, ts=ts, service=service, type=ctype, summary="s", author="a", diff="--- a\n+++ b\n")
    if ctype == "deploy":
        base.update(from_version="1.4.1", to_version="1.5.0")
    else:
        base.update(key="db_pool_size", old=8, new=2)
    base.update(kw)
    return Change(**base)


def queue_rows(degraded_from: float | None, value: float = 100.0, until: float = 120.0, start: float = 0.0) -> list[dict]:
    rows, t = [], start
    while t <= until:
        v = value if degraded_from is not None and t >= degraded_from else 0.0
        rows.append({"ts": t, "service": "worker", "name": "queue_depth", "labels": {}, "value": v})
        t += 2.0
    return rows


def tel(degraded_from: float | None = 84.0, now: float = 120.0, changes=None, logs=None) -> T.Telemetry:
    alert = Alert(name="WorkerQueueBacklog", severity="page", service="worker", summary="queue growing", fired_at=now - 10)
    return T.Telemetry(logs or [], queue_rows(degraded_from, until=now), changes if changes is not None else [change()], alert, now)


class StubEnv(Env):
    """Recovers (queue back to 0) only if `recovers` is True after any execute()."""

    def __init__(self, recovers=True, changes=None, mode="full"):
        super().__init__(mode)
        self._changes = changes if changes is not None else [change(service="worker", ctype="config")]
        self._tel = tel(changes=self._changes)
        self.recovers = recovers
        self.log: list[str] = []

    @property
    def tel(self):
        return self._tel

    def execute(self, action: Action):
        self.log.append("execute:" + action.type)
        self.executed.append(action)
        return {"applied": True}

    def watch(self, watch_s=15.0):
        return tel(degraded_from=None if self.recovers else 84.0, now=135.0,
                   changes=self._changes)
