"""Recovery verification: did the action actually fix it, judged by criteria declared BEFORE acting."""
from __future__ import annotations

from opspilot import telemetry as T
from opspilot.schemas import Criterion

WINDOW_S = 10.0


def measure(tel: T.Telemetry, metric: str) -> float | None:
    return T.SERIES[metric][1](tel.metrics, tel.now - WINDOW_S, tel.now)


def snapshot_values(tel: T.Telemetry, criteria: list[Criterion]) -> dict[str, float | None]:
    return {c.metric: measure(tel, c.metric) for c in criteria}


def verify(after: T.Telemetry, criteria: list[Criterion], before: dict[str, float | None]) -> dict:
    """recovered: every criterion holds. regressed: a criterion is worse than before the action by >25%.
    not_recovered: criteria fail, or there is no data to judge (we never assume recovery)."""
    if not criteria:
        return {"verdict": "unverified", "checks": [], "note": "no success criteria were declared"}
    checks, regressed = [], False
    for c in criteria:
        v = measure(after, c.metric)
        ok = v is not None and (v < c.value if c.op == "<" else v > c.value)
        b = before.get(c.metric)
        if v is not None and b is not None and c.op == "<" and v > b * 1.25 + 1e-9 and not ok:
            regressed = True
        checks.append({"metric": c.metric, "op": c.op, "threshold": c.value,
                       "before": None if b is None else round(b, 3), "after": None if v is None else round(v, 3), "ok": ok})
    verdict = "recovered" if all(c["ok"] for c in checks) else "regressed" if regressed else "not_recovered"
    return {"verdict": verdict, "checks": checks}
