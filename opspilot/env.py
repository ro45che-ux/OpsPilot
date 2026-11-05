"""The world an agent investigates: typed read-only tools over telemetry, plus controlled execution.

Two implementations share every tool:
  * ReplayEnv: serves frozen snapshots. Deterministic and free, used for evals. Executing an action
    switches to the recorded branch that matches what was done (see recorder.py).
  * LiveEnv:   reads telemetry from the running local testbed and really executes actions.

Every tool call is stored as Evidence with an ID. Findings must cite those IDs; a validator rejects
claims that point at nothing. Nothing here exposes ground truth or harness.json.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from opspilot import telemetry as T
from opspilot.scenarios import load as load_scenario
from opspilot.schemas import Action, Alert, Change, Scenario
from opspilot.snapshot import Snapshot
from testbed.common.state import read_jsonl
from testbed.releases.catalog import BASELINE

WATCH_S = 15.0  # how long recovery is observed after an action


@dataclass
class Evidence:
    id: str
    tool: str
    args: dict
    result: Any
    t: float = field(default_factory=time.time)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[[dict], Any]


def action_key(a: Action) -> tuple:
    return (a.type, a.service, a.to_version, a.key, json.dumps(a.value) if a.value is not None else None)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


class Env:
    mode: str = "full"

    def __init__(self, mode: str = "full"):
        self.pace = 0.0       # seconds to pause after each step (UI demos only)
        self.on_call = None   # fn(evidence_id, tool, args, result) after every tool call
        self.on_note = None   # fn(text) for agent reasoning text
        self.mode = mode
        self.evidence: dict[str, Evidence] = {}
        self.executed: list[Action] = []

    # subclasses ------------------------------------------------------------------------------
    @property
    def tel(self) -> T.Telemetry:  # current view
        raise NotImplementedError

    def refresh(self) -> None:
        pass

    def execute(self, action: Action) -> dict:
        raise NotImplementedError

    def watch(self, watch_s: float = WATCH_S) -> T.Telemetry:
        """Telemetry after the executed actions have had time to take effect."""
        raise NotImplementedError

    # shared ----------------------------------------------------------------------------------
    @property
    def alert(self) -> Alert:
        return self.tel.alert

    def alert_summary(self) -> dict:
        a = self.tel.alert
        return {"name": a.name, "severity": a.severity, "service": a.service, "summary": a.summary,
                "fired_ago_s": self.tel.ago(a.fired_at)}

    def config_of(self, service: str) -> dict:
        tel = self.tel
        version = BASELINE[service]["version"]
        cfg = dict(BASELINE[service]["config"])
        versions, hist = [], {}
        for c in sorted(tel.changes, key=lambda c: c.ts):
            if c.service != service:
                continue
            if c.type == "deploy":
                versions += [v for v in (c.from_version, c.to_version) if v and v not in versions]
                version = c.to_version
            else:
                cfg[c.key] = c.new
                hist.setdefault(c.key, []).append({"change_id": c.id, "old": c.old, "new": c.new, "ago_s": tel.ago(c.ts)})
        return {"service": service, "version": version, "known_versions": [v for v in versions if v != version],
                "config": cfg, "config_history": hist}

    # tools -----------------------------------------------------------------------------------
    def tools(self) -> list[ToolSpec]:
        svc = {"type": "string", "enum": ["api", "worker"]}
        lvl = {"type": "string", "enum": ["INFO", "WARNING", "ERROR"], "description": "minimum level"}
        full = self.mode == "full"
        specs = [
            ToolSpec("list_changes", "List recent deploys, config changes and flag changes (newest first), "
                     "without diffs. Environmental events (third-party outages, bad data) are NOT recorded here.",
                     _obj({"service": svc, "limit": {"type": "integer", "default": 15}}), self._list_changes),
            ToolSpec("get_change_diff", "Return the commit message and unified diff for one change id.",
                     _obj({"change_id": {"type": "string"}}, ["change_id"]), self._get_diff),
            ToolSpec("get_config", "Current version, config values, config change history and previous "
                     "versions for a service. Use it to find the value to revert to.",
                     _obj({"service": svc}, ["service"]), lambda a: self.config_of(a["service"])),
        ]
        if full:
            specs += [
                ToolSpec("find_anomalies", "Detect metrics that moved away from their early-window baseline, "
                         "earliest onset first. Start here. Series: " + ", ".join(T.SERIES),
                         _obj({}), self._find_anomalies),
                ToolSpec("correlate_changes", "Rank changes by how closely they precede the first anomaly. Timing and "
                         "service only: a recent change is a lead, not proof. Read its diff before blaming it.",
                         _obj({}), self._correlate),
                ToolSpec("log_patterns", "Cluster logs into templates with baseline-vs-recent counts, trend "
                         "(new/rising/stable) and numeric field shifts (e.g. db_queries 2->31).",
                         _obj({"service": svc, "min_level": lvl, "contains": {"type": "string"}}), self._log_patterns),
                ToolSpec("search_logs", "Raw log lines, newest first. Filter by pattern_id from log_patterns, "
                         "substring, level or service.",
                         _obj({"service": svc, "min_level": lvl, "contains": {"type": "string"},
                               "pattern_id": {"type": "string"}, "limit": {"type": "integer", "default": 8}}),
                         self._search_logs),
                ToolSpec("query_series", "A derived series over time (value per 2s step, downsampled), with its "
                         "baseline mean. Series: " + "; ".join(f"{k} = {v[0]}" for k, v in T.SERIES.items()),
                         _obj({"series": {"type": "string", "enum": list(T.SERIES)},
                               "lookback_s": {"type": "integer", "default": 60}}, ["series"]), self._query_series),
            ]
        else:
            specs.append(ToolSpec("search_logs", "Raw log lines, newest first, with optional substring/level/service "
                                  "filter.", _obj({"service": svc, "min_level": lvl, "contains": {"type": "string"},
                                                   "limit": {"type": "integer", "default": 20}}), self._search_logs))
        specs.append(ToolSpec("query_metric_raw", "Raw scraped samples for one metric (counters are cumulative). "
                              "Metrics: " + ", ".join(T.RAW_METRICS),
                              _obj({"metric": {"type": "string"}, "service": svc,
                                    "lookback_s": {"type": "integer", "default": 60},
                                    "label_filter": {"type": "object", "description": "exact label matches"}},
                                   ["metric", "service"]), self._query_raw))
        return specs

    def call(self, name: str, args: dict | None = None) -> dict:
        self.refresh()
        spec = next((s for s in self.tools() if s.name == name), None)
        eid = f"ev-{len(self.evidence) + 1:03d}"
        if spec is None:
            result: Any = {"error": f"unknown tool '{name}'"}
        else:
            try:
                result = spec.fn(args or {})
            except (KeyError, ValueError, TypeError) as e:
                result = {"error": f"bad arguments: {type(e).__name__}: {e}"}
        self.evidence[eid] = Evidence(eid, name, args or {}, result)
        if self.on_call:
            self.on_call(eid, name, args or {}, result)
        if self.pace:
            time.sleep(self.pace)
        return {"evidence_id": eid, "result": result}

    # tool implementations --------------------------------------------------------------------
    def _change_row(self, c: Change) -> dict:
        row = {"id": c.id, "ago_s": self.tel.ago(c.ts), "service": c.service, "type": c.type,
               "summary": c.summary, "author": c.author}
        if c.type == "deploy":
            row.update(from_version=c.from_version, to_version=c.to_version, **({"rollback": True} if c.rollback else {}))
        else:
            row.update(key=c.key, old=c.old, new=c.new)
        return row

    def _list_changes(self, a: dict) -> dict:
        rows = [c for c in sorted(self.tel.changes, key=lambda c: -c.ts) if not a.get("service") or c.service == a["service"]]
        return {"changes": [self._change_row(c) for c in rows[: int(a.get("limit", 15))]]}

    def _get_diff(self, a: dict) -> dict:
        c = next((c for c in self.tel.changes if c.id == a["change_id"]), None)
        if c is None:
            raise KeyError(f"no change {a['change_id']}")
        return {**self._change_row(c), "diff": c.diff or "(no diff: rollback or revert)"}

    def _find_anomalies(self, _a: dict) -> dict:
        an = T.detect_anomalies(self.tel)
        return {"baseline_assumption": "earliest 25% of the observation window is healthy",
                "anomalies": [{"series": x.series, "onset_ago_s": self.tel.ago(x.onset_ts), "baseline": round(x.baseline, 3),
                               "current": round(x.current, 3), "peak": round(x.peak, 3)} for x in an],
                **({} if an else {"note": "nothing deviated; the symptom may be below thresholds or not in these series"})}

    def _correlate(self, _a: dict) -> dict:
        an = T.detect_anomalies(self.tel)
        cands = T.correlate_changes(self.tel, an)
        return {"first_anomaly": an[0].series if an else None,
                "candidates": [{**self._change_row(c.change), "lead_s": c.lead_s, "score": c.score,
                                "service_has_anomaly": c.same_service_anomaly} for c in cands[:8]]}

    def _log_patterns(self, a: dict) -> dict:
        return {"patterns": T.log_patterns(self.tel, a.get("service"), a.get("min_level", "INFO"), a.get("contains"))}

    def _search_logs(self, a: dict) -> dict:
        lim = min(int(a.get("limit", 8 if self.mode == "full" else 20)), 40)
        return {"lines": T.search_logs(self.tel, a.get("service"), a.get("min_level", "INFO"), a.get("contains"),
                                       a.get("pattern_id"), lim)}

    def _query_series(self, a: dict) -> dict:
        name = a["series"]
        if name not in T.SERIES:
            raise KeyError(f"unknown series {name}")
        lookback = float(a.get("lookback_s", 60))
        pts = self.tel.series(name, lookback)
        b_end = self.tel.baseline_end()
        base = [v for t, v in self.tel.series(name) if v is not None and t <= b_end]
        stride = max(1, len(pts) // 15)
        return {"series": name, "points": [{"ago_s": self.tel.ago(t), "value": None if v is None else round(v, 3)}
                                           for t, v in pts[::stride]],
                "baseline_mean": round(sum(base) / len(base), 3) if base else None,
                "latest": None if not pts or pts[-1][1] is None else round(pts[-1][1], 3)}

    def _query_raw(self, a: dict) -> dict:
        metric, service = a["metric"], a["service"]
        want = a.get("label_filter") or {}
        lo = self.tel.now - float(a.get("lookback_s", 60))
        sets = self.tel.metrics._match(metric, service=service, **want)
        if not sets:
            return {"metric": metric, "series": [], "note": f"no samples; known metrics: {T.RAW_METRICS}"}
        out = []
        for labels, pts in sorted(sets, key=lambda s: sorted(dict(s[0]).items()))[:8]:
            w = [(t, v) for t, v in pts if t >= lo]
            stride = max(1, len(w) // 10)
            out.append({"labels": {k: v for k, v in labels if k != "service"},
                        "points": [{"ago_s": self.tel.ago(t), "value": round(v, 4)} for t, v in w[::stride]]})
        return {"metric": metric, "series": out}


# ---- replay ----------------------------------------------------------------------------------
class ReplayEnv(Env):
    def __init__(self, scenario_id: str, snapshots_root: Path, mode: str = "full"):
        super().__init__(mode)
        self.scenario: Scenario = load_scenario(scenario_id)
        root = Path(snapshots_root) / scenario_id
        self.nofix = Snapshot(root / "nofix")
        self.fix = Snapshot(root / "fix") if (root / "fix" / "meta.json").exists() else None
        # The agent is dropped into the incident at a fixed point after the alert fires.
        self.t_inv = self.nofix.harness["fault_start_ts"] + self.scenario.timeline.fix_at_s
        self._tel = self._view(self.nofix, self.t_inv)

    @staticmethod
    def _view(s: Snapshot, now: float) -> T.Telemetry:
        return T.Telemetry(s.logs, s.metric_rows, s.changes, s.alert, now)

    @property
    def tel(self) -> T.Telemetry:
        return self._tel

    def execute(self, action: Action) -> dict:
        self.executed.append(action)
        time.sleep(self.pace * 2)
        return {"applied": True, "note": f"{action.type} applied (replay)"}

    def _recovers(self) -> bool:
        gt = self.scenario.ground_truth
        done = {action_key(a) for a in self.executed}
        want = {action_key(a) for a in gt.correct_actions}
        ok = {action_key(a) for a in gt.acceptable_actions}
        return self.fix is not None and (want <= done or bool(ok & done))

    def watch(self, watch_s: float = WATCH_S) -> T.Telemetry:
        time.sleep(self.pace * 2)
        if self._recovers():
            f, n = self.fix, self.nofix
            anchor = f.harness["fix_applied_ts"]
            off = self.t_inv - anchor

            def splice(base: list[dict], fixed: list[dict]) -> list[dict]:
                return [r for r in base if r["ts"] <= self.t_inv] + [
                    {**r, "ts": round(r["ts"] + off, 3)} for r in fixed if r["ts"] > anchor]

            now = min(self.t_inv + watch_s, max(r["ts"] for r in f.metric_rows) + off)
            changes = [c for c in n.changes if c.ts <= self.t_inv] + [
                c.model_copy(update={"ts": round(c.ts + off, 3)}) for c in f.changes if c.ts > anchor]
            return T.Telemetry(splice(n.logs, f.logs), splice(n.metric_rows, f.metric_rows), changes, n.alert, now)
        now = min(self.t_inv + watch_s, max(r["ts"] for r in self.nofix.metric_rows))
        return self._view(self.nofix, now)


# ---- live ------------------------------------------------------------------------------------
class LiveEnv(Env):
    """Telemetry from a running LocalStack. `scraper_file` is the recorder's metrics.jsonl."""

    def __init__(self, stack, state: Path, scraper_file: Path, alert: Alert, mode: str = "full"):
        super().__init__(mode)
        self.stack, self.state, self.scraper_file, self._alert = stack, Path(state), Path(scraper_file), alert
        self._tel: T.Telemetry | None = None
        self.refresh()

    def refresh(self) -> None:
        logs: list[dict] = []
        for p in sorted((self.state / "logs").glob("*.jsonl")):
            logs += read_jsonl(p)
        changes = [Change.model_validate(c) for c in read_jsonl(self.state / "changes.jsonl")]
        self._tel = T.Telemetry(logs, read_jsonl(self.scraper_file), changes, self._alert, time.time())

    @property
    def tel(self) -> T.Telemetry:
        return self._tel

    def execute(self, action: Action) -> dict:
        from opspilot import injector
        injector.execute_action(action, self.state, self.stack)
        self.executed.append(action)
        return {"applied": True, "note": f"{action.type} applied (live)"}

    def watch(self, watch_s: float = WATCH_S) -> T.Telemetry:
        time.sleep(watch_s)
        self.refresh()
        return self._tel
