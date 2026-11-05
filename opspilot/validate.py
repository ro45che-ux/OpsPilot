"""Check that a recorded snapshot actually shows what its scenario's ground truth claims.

A scenario is only a fair test if (a) the symptom is absent before the fault, (b) present by the
end of the nofix run, (c) gone after the correct fix, and (d) every required evidence item exists
in the telemetry. Anything else means the benchmark would grade the agent against a lie.
"""
from __future__ import annotations

from pathlib import Path

from opspilot.scenarios import load
from opspilot.schemas import Matcher, Scenario
from opspilot.snapshot import Snapshot, holds

WARMUP_S = 6   # ignore the first seconds of each run while pools and caches warm up
WINDOW_S = 10  # width of the windows used to evaluate criteria


def _log_matches(snap: Snapshot, m: Matcher) -> bool:
    for r in snap.logs:
        if m.service and r.get("service") != m.service:
            continue
        if m.msg_contains and m.msg_contains not in f"{r.get('msg', '')} {r.get('error', '')}":
            continue
        if m.field is not None and not (isinstance(r.get(m.field), (int, float)) and r[m.field] >= (m.min or 0)):
            continue
        return True
    return False


def check_evidence(sc: Scenario, snap: Snapshot) -> list[str]:
    problems = []
    change_ids = {c.id for c in snap.changes}
    names = snap.metrics.names()
    for m in sc.ground_truth.required_evidence:
        if m.source == "change" and m.ref not in change_ids:
            problems.append(f"evidence change {m.ref} not in changes.jsonl")
        elif m.source == "log" and not _log_matches(snap, m):
            problems.append(f"evidence log not found: {m.model_dump(exclude_none=True)}")
        elif m.source == "metric" and m.name not in names:
            problems.append(f"evidence metric {m.name} never recorded")
    return problems


def validate_snapshot(sc: Scenario, snap: Snapshot) -> tuple[list[str], dict]:
    problems = check_evidence(sc, snap)
    start, end = snap.meta["start_ts"], snap.meta["end_ts"]
    fault = snap.harness["fault_start_ts"]
    fix = snap.harness["fix_applied_ts"]
    observed: dict[str, dict[str, float | None]] = {}
    if snap.alert.fired_at <= fault:
        problems.append("alert fires before the fault starts")

    for c in sc.ground_truth.success_criteria:
        vals = {
            "baseline": snap.derived(c.metric, start + WARMUP_S, fault),
            "before_fix_or_end": snap.derived(c.metric, (fix or end) - WINDOW_S, fix or end),
        }
        if fix:
            vals["after_fix"] = snap.derived(c.metric, end - WINDOW_S, end)
        observed[c.metric] = vals

        def need(label: str, expect_ok: bool) -> None:
            v = vals[label]
            if v is None:
                problems.append(f"{c.metric} [{label}]: no data")
            elif holds(c.op, v, c.value) != expect_ok:
                problems.append(f"{c.metric} [{label}] = {v:.3f}, expected criterion "
                                f"'{c.op} {c.value}' to {'hold' if expect_ok else 'be violated'}")

        need("baseline", True)
        need("before_fix_or_end", False)
        if fix:
            need("after_fix", True)
    return problems, observed


def validate_scenario(scenario_id: str, snapshots_root: Path) -> tuple[list[str], dict]:
    sc = load(scenario_id)
    problems: list[str] = []
    observed: dict = {}
    branches = ["nofix"] + (["fix"] if sc.has_fix_branch else [])
    for b in branches:
        path = Path(snapshots_root) / scenario_id / b
        if not (path / "meta.json").exists():
            problems.append(f"[{b}] snapshot missing: {path}")
            continue
        p, o = validate_snapshot(sc, Snapshot(path))
        problems += [f"[{b}] {x}" for x in p]
        observed[b] = o
    return problems, observed
