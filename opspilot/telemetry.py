"""Deterministic telemetry analysis that runs BEFORE the LLM sees anything.

  * derived series (error rate, p99, queries/request, ...) computed from raw Prometheus samples
  * anomaly-onset detection against an early-window baseline
  * log template clustering with baseline-vs-recent comparison (so 40 rps of INFO lines collapse
    into a handful of patterns, and "db_queries went 2 -> 31" is visible without reading 3000 lines)
  * change correlation: which recent deploy/config/flag change lines up with the first anomaly

Everything here only looks at data with ts <= `now`; that is what makes replay honest.
"""
from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from opspilot.schemas import Alert, Change
from opspilot.snapshot import MetricStore

STEP_S = 2.0       # evaluation step; matches the scrape interval
WINDOW_S = 6.0     # rate / quantile window for each derived point
WARMUP_S = 4.0     # ignore points this soon after data begins (pools and caches are cold)
BASELINE_FRAC = 0.25  # the earliest 25% of the observation window is assumed healthy


# ---- derived series --------------------------------------------------------------------------
def _err(m: MetricStore, a, b):
    tot = m.rate("http_requests_total", a, b, service="api")
    bad = m.rate("http_requests_total", a, b, service="api", status=lambda s: s is not None and s.startswith("5"))
    return bad / tot if tot > 0 else None


def _per_req(m: MetricStore, a, b):
    tot = m.rate("http_requests_total", a, b, service="api")
    return m.rate("db_queries_total", a, b, service="api") / tot if tot > 0 else None


def _mb(v):
    return None if v is None else v / 1e6


SERIES: dict[str, tuple[str, Callable[[MetricStore, float, float], float | None], tuple[str, ...], float]] = {
    # name: (description, fn(store, t0, t1), raw metrics it is derived from, absolute change floor)
    "api.error_rate": ("fraction of api requests returning 5xx", _err, ("http_requests_total",), 0.01),
    "api.p99_latency_s": ("api p99 request latency in seconds",
                          lambda m, a, b: m.histogram_quantile(0.99, "http_request_duration_seconds", a, b, service="api"),
                          ("http_request_duration_seconds_bucket",), 0.15),
    "api.request_rate": ("api requests per second", lambda m, a, b: m.rate("http_requests_total", a, b, service="api"),
                         ("http_requests_total",), 1e9),  # informational; never flagged
    "api.db_queries_per_request": ("database round trips per api request", _per_req,
                                   ("db_queries_total", "http_requests_total"), 2.0),
    "api.db_pool_in_use": ("db connections checked out (max in window)",
                           lambda m, a, b: m.gauge_max("db_pool_in_use", a, b, service="api"), ("db_pool_in_use",), 2.0),
    "api.db_pool_timeouts_per_s": ("timeouts waiting for a db connection per second",
                                   lambda m, a, b: m.rate("db_pool_timeouts_total", a, b, service="api"),
                                   ("db_pool_timeouts_total",), 0.5),
    "api.memory_mb": ("api Python heap in use, MB (max in window)",
                      lambda m, a, b: _mb(m.gauge_max("process_heap_bytes", a, b, service="api")),
                      ("process_heap_bytes", "process_resident_memory_bytes"), 8.0),
    "api.cpu_percent": ("api process CPU percent (max in window)",
                        lambda m, a, b: m.gauge_max("process_cpu_percent", a, b, service="api"),
                        ("process_cpu_percent",), 30.0),
    "api.payments_p99_s": ("payments provider call p99 latency in seconds",
                           lambda m, a, b: m.histogram_quantile(0.99, "payments_request_duration_seconds", a, b, service="api"),
                           ("payments_request_duration_seconds_bucket",), 0.2),
    "worker.queue_depth": ("pending jobs in the queue", lambda m, a, b: m.gauge_last("queue_depth", a, b, service="worker"),
                           ("queue_depth",), 5.0),
    "worker.queue_oldest_age_s": ("age in seconds of the oldest pending job",
                                  lambda m, a, b: m.gauge_max("queue_oldest_age_seconds", a, b, service="worker"),
                                  ("queue_oldest_age_seconds",), 3.0),
    "worker.job_failures_per_s": ("worker job attempts that raised, per second",
                                  lambda m, a, b: m.rate("jobs_failed_total", a, b, service="worker"),
                                  ("jobs_failed_total",), 0.3),
}
RAW_METRICS = sorted({m for _, _, raws, _ in SERIES.values() for m in raws})


@dataclass
class Telemetry:
    logs: list[dict]
    metric_rows: list[dict]
    changes: list[Change]
    alert: Alert
    now: float
    metrics: MetricStore = field(init=False)
    data_start: float = field(init=False)

    def __post_init__(self):
        self.logs = [r for r in self.logs if r["ts"] <= self.now]
        self.metric_rows = [r for r in self.metric_rows if r["ts"] <= self.now]
        self.changes = [c for c in self.changes if c.ts <= self.now]
        self.metrics = MetricStore(self.metric_rows)
        self.data_start = min((r["ts"] for r in self.metric_rows), default=self.now)

    def ago(self, ts: float) -> float:
        return round(self.now - ts, 1)

    def baseline_end(self) -> float:
        first = self.data_start + WARMUP_S + WINDOW_S
        return first + BASELINE_FRAC * max(0.0, self.now - first)

    # series ---------------------------------------------------------------------------------
    def series(self, name: str, lookback_s: float | None = None, step_s: float = STEP_S) -> list[tuple[float, float | None]]:
        _, fn, _, _ = SERIES[name]
        lo = self.data_start + WARMUP_S + WINDOW_S
        if lookback_s is not None:
            lo = max(lo, self.now - lookback_s)
        out, t = [], lo
        while t <= self.now + 1e-6:
            out.append((t, fn(self.metrics, t - WINDOW_S, t)))
            t += step_s
        return out


# ---- anomaly detection -----------------------------------------------------------------------
@dataclass
class Anomaly:
    series: str
    onset_ts: float
    baseline: float
    current: float
    peak: float


def detect_anomalies(tel: Telemetry, names: list[str] | None = None, min_run: int = 3) -> list[Anomaly]:
    """Increases that persist >= min_run consecutive steps beyond max(4 sigma, 30% of mean, floor)."""
    out = []
    b_end = tel.baseline_end()
    for name in names or list(SERIES):
        floor = SERIES[name][3]
        pts = [(t, v) for t, v in tel.series(name) if v is not None]
        base = [v for t, v in pts if t <= b_end]
        rest = [(t, v) for t, v in pts if t > b_end]
        if len(base) < 3 or len(rest) < min_run:
            continue
        mean = statistics.fmean(base)
        sd = statistics.pstdev(base)
        thr = mean + max(4 * sd, 0.3 * abs(mean), floor)
        run: list[tuple[float, float]] = []
        for t, v in rest:
            run = run + [(t, v)] if v > thr else []
            if len(run) >= min_run:
                vals = [v for _, v in rest]
                # the point at t reflects [t-W, t]; the rise began about halfway into that window
                out.append(Anomaly(name, run[0][0] - WINDOW_S / 2, mean, rest[-1][1], max(vals)))
                break
    return sorted(out, key=lambda a: a.onset_ts)


# ---- change correlation ----------------------------------------------------------------------
@dataclass
class Candidate:
    change: Change
    lead_s: float
    score: float
    same_service_anomaly: bool


def correlate_changes(tel: Telemetry, anomalies: list[Anomaly], horizon_s: float = 900.0) -> list[Candidate]:
    """Rank changes that precede the first anomaly. Timing + service only: a good candidate list still
    needs a human (or the agent) to read the diff, because a recent change is not necessarily the cause."""
    if not anomalies:
        return []
    onset = anomalies[0].onset_ts
    anomalous_services = {a.series.split(".")[0] for a in anomalies}
    out = []
    for c in tel.changes:
        lead = onset - c.ts
        if lead < -5 or lead > horizon_s:
            continue
        same = c.service in anomalous_services
        out.append(Candidate(c, round(lead, 1), round(math.exp(-max(lead, 0) / 120.0) * (1.0 if same else 0.5), 3), same))
    return sorted(out, key=lambda x: -x.score)


# ---- log clustering --------------------------------------------------------------------------
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
FIELD_STATS = ("duration_ms", "db_queries", "attempt")


def normalize(text: str) -> str:
    return _NUM.sub("<*>", _HEX.sub("<*>", _QUOTED.sub("<*>", text or "")))


def template_of(r: dict) -> str:
    parts = [r.get("level", ""), r.get("service", ""), normalize(r.get("msg", ""))]
    if "route" in r:
        parts.append(f"route={r['route']}")
    if "status" in r:
        parts.append(f"status={r['status']}")
    if r.get("error"):
        parts.append("err=" + normalize(str(r["error"])).split("\n")[0][:140])
    return " | ".join(parts)


def pattern_id(template: str) -> str:
    return "pat-" + hashlib.md5(template.encode()).hexdigest()[:8]


def _stats(vals: list[float]) -> dict:
    return {"mean": round(statistics.fmean(vals), 2), "max": round(max(vals), 2)} if vals else {}


def log_patterns(tel: Telemetry, service: str | None = None, min_level: str = "INFO", contains: str | None = None,
                 limit: int = 12) -> list[dict]:
    rank = {"INFO": 0, "WARNING": 1, "ERROR": 2}
    b_end = tel.baseline_end()
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in tel.logs:
        if service and r.get("service") != service:
            continue
        if rank.get(r.get("level"), 0) < rank[min_level]:
            continue
        if contains and contains.lower() not in (str(r.get("msg", "")) + " " + str(r.get("error", ""))).lower():
            continue
        groups[template_of(r)].append(r)
    base_len = max(b_end - tel.data_start, 1.0)
    recent_len = max(tel.now - b_end, 1.0)
    out = []
    for tpl, rows in groups.items():
        base = [r for r in rows if r["ts"] <= b_end]
        recent = [r for r in rows if r["ts"] > b_end]
        b_rate, r_rate = len(base) / base_len, len(recent) / recent_len
        if not base and recent:
            trend = "new"
        elif r_rate > 2 * b_rate and recent:
            trend = f"rising x{r_rate / b_rate:.1f}"
        elif b_rate > 2 * r_rate and base:
            trend = f"falling x{b_rate / max(r_rate, 1e-9):.1f}" if recent else "gone"
        else:
            trend = "stable"
        fields, shifted = {}, []
        for f in FIELD_STATS:
            bv = [r[f] for r in base if isinstance(r.get(f), (int, float))]
            rv = [r[f] for r in recent if isinstance(r.get(f), (int, float))]
            if bv or rv:
                fields[f] = {"baseline": _stats(bv), "recent": _stats(rv)}
                if bv and rv and statistics.fmean(rv) > 2 * statistics.fmean(bv) + 0.5:
                    shifted.append(f"{f} {statistics.fmean(bv):.0f}->{statistics.fmean(rv):.0f}")
        ex = max(rows, key=lambda r: r["ts"])
        out.append({
            "pattern_id": pattern_id(tpl), "template": tpl, "count": len(rows),
            "first_seen_ago_s": tel.ago(rows[0]["ts"]), "last_seen_ago_s": tel.ago(ex["ts"]),
            "baseline_count": len(base), "recent_count": len(recent), "trend": trend,
            **({"field_shift": shifted} if shifted else {}), "fields": fields,
            "example": _trim(ex),
        })

    def interesting(p: dict) -> tuple:
        lvl = 2 if " ERROR " in f" {p['template'].split(' | ')[0]} " else 1 if p["template"].startswith("WARNING") else 0
        moved = p["trend"] != "stable" or "field_shift" in p
        return (-int(moved), -lvl, -p["count"])

    return sorted(out, key=interesting)[:limit]


def _trim(r: dict) -> dict:
    r = dict(r)
    if r.get("stack"):
        lines = [l.strip() for l in r["stack"].strip().splitlines()]
        r["stack"] = " / ".join(lines[-3:])[:300]
    return r


def search_logs(tel: Telemetry, service: str | None = None, min_level: str = "INFO", contains: str | None = None,
                pattern: str | None = None, limit: int = 20, newest_first: bool = True) -> list[dict]:
    rank = {"INFO": 0, "WARNING": 1, "ERROR": 2}
    out = []
    for r in reversed(tel.logs) if newest_first else tel.logs:
        if service and r.get("service") != service:
            continue
        if rank.get(r.get("level"), 0) < rank[min_level]:
            continue
        if contains and contains.lower() not in (str(r.get("msg", "")) + " " + str(r.get("error", ""))).lower():
            continue
        if pattern and pattern_id(template_of(r)) != pattern:
            continue
        out.append({**_trim(r), "ago_s": tel.ago(r["ts"])})
        if len(out) >= limit:
            break
    return out


# ---- chart export (UI) -----------------------------------------------------------------------
CHART_SERIES = ["api.error_rate", "api.p99_latency_s", "api.request_rate", "api.db_queries_per_request",
                "api.db_pool_in_use", "api.db_pool_timeouts_per_s", "api.memory_mb", "api.cpu_percent",
                "api.payments_p99_s", "worker.queue_depth", "worker.queue_oldest_age_s", "worker.job_failures_per_s"]


def chart_data(tel: Telemetry) -> dict:
    """Everything the UI needs to draw the incident charts; series are [ts, value] pairs."""
    anomalies = {a.series: a for a in detect_anomalies(tel)}
    out = {}
    for name in CHART_SERIES:
        pts = [[round(t, 1), None if v is None else round(v, 4)] for t, v in tel.series(name)]
        if not any(v is not None for _, v in pts):
            continue
        a = anomalies.get(name)
        out[name] = {"desc": SERIES[name][0], "points": pts, "baseline": None if a is None else round(a.baseline, 4),
                     "onset": None if a is None else round(a.onset_ts, 1), "anomalous": a is not None}
    return {"series": out, "now": round(tel.now, 1), "alert_ts": tel.alert.fired_at or None,
            "changes": [{"ts": round(c.ts, 1), "id": c.id, "type": c.type, "service": c.service, "summary": c.summary,
                         "rollback": c.rollback} for c in tel.changes]}
