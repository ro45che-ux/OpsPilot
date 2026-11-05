"""Read side of a recorded snapshot, plus derived metrics (rate, error ratio, histogram quantile).

This is intentionally small: it is what the agent's telemetry tools will be built on, and what
`validate` uses to prove a recorded scenario really shows the symptom its ground truth claims.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from opspilot.schemas import Alert, Change
from testbed.common.state import read_jsonl


class MetricStore:
    def __init__(self, rows: list[dict]):
        # (name, frozenset(labels+service)) -> sorted [(ts, value)]
        self._series: dict[tuple[str, frozenset], list[tuple[float, float]]] = defaultdict(list)
        for r in rows:
            labels = frozenset({**r["labels"], "service": r["service"]}.items())
            self._series[(r["name"], labels)].append((r["ts"], r["value"]))
        for s in self._series.values():
            s.sort()

    def names(self) -> set[str]:
        return {n for n, _ in self._series}

    def _match(self, name: str, **want) -> list[tuple[frozenset, list[tuple[float, float]]]]:
        out = []
        for (n, labels), pts in self._series.items():
            if n != name:
                continue
            d = dict(labels)
            if all((v(d.get(k)) if callable(v) else d.get(k) == v) for k, v in want.items()):
                out.append((labels, pts))
        return out

    def rate(self, name: str, t0: float, t1: float, **want) -> float:
        """Sum of per-series increase/second over [t0, t1] (counter resets ignored)."""
        total = 0.0
        for _, pts in self._match(name, **want):
            w = [p for p in pts if t0 <= p[0] <= t1]
            if len(w) >= 2 and w[-1][0] > w[0][0]:
                total += max(0.0, w[-1][1] - w[0][1]) / (w[-1][0] - w[0][0])
        return total

    def gauge_max(self, name: str, t0: float, t1: float, **want) -> float | None:
        vals = [v for _, pts in self._match(name, **want) for t, v in pts if t0 <= t <= t1]
        return max(vals) if vals else None

    def gauge_last(self, name: str, t0: float, t1: float, **want) -> float | None:
        best = None
        for _, pts in self._match(name, **want):
            for t, v in pts:
                if t0 <= t <= t1 and (best is None or t >= best[0]):
                    best = (t, v)
        return best[1] if best else None

    def histogram_quantile(self, q: float, base: str, t0: float, t1: float, **want) -> float | None:
        """Quantile of observations that arrived within [t0, t1], across all matching series."""
        buckets: dict[float, float] = defaultdict(float)
        for labels, pts in self._match(base + "_bucket", **want):
            w = [p for p in pts if t0 <= p[0] <= t1]
            if len(w) >= 2:
                le = dict(labels)["le"]
                buckets[float("inf") if le == "+Inf" else float(le)] += max(0.0, w[-1][1] - w[0][1])
        if not buckets:
            return None
        edges = sorted(buckets)
        total = buckets[edges[-1]]
        if total <= 0:
            return None
        target, prev_edge, prev_count = q * total, 0.0, 0.0
        for e in edges:
            c = buckets[e]
            if c >= target:
                if e == float("inf"):
                    return prev_edge
                span = c - prev_count
                return prev_edge + (e - prev_edge) * ((target - prev_count) / span if span else 1.0)
            prev_edge, prev_count = e, c
        return prev_edge


class Snapshot:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.meta = json.loads((self.path / "meta.json").read_text())
        self.harness = json.loads((self.path / "harness.json").read_text())  # harness-only
        self.alert = Alert.model_validate_json((self.path / "alert.json").read_text())
        self.changes = [Change.model_validate(c) for c in read_jsonl(self.path / "changes.jsonl")]
        self.logs = read_jsonl(self.path / "logs.jsonl")
        self.metric_rows = read_jsonl(self.path / "metrics.jsonl")
        self.metrics = MetricStore(self.metric_rows)

    # derived metrics used by success criteria and alert checks ---------------------------------
    def derived(self, metric: str, t0: float, t1: float) -> float | None:
        service, name = metric.split(".")
        if name == "error_rate":
            total = self.metrics.rate("http_requests_total", t0, t1, service=service)
            bad = self.metrics.rate("http_requests_total", t0, t1, service=service,
                                    status=lambda s: s is not None and s.startswith("5"))
            return bad / total if total > 0 else None
        if name == "p99_latency_s":
            return self.metrics.histogram_quantile(0.99, "http_request_duration_seconds", t0, t1, service=service)
        if name == "memory_mb":
            v = self.metrics.gauge_max("process_heap_bytes", t0, t1, service=service)
            return v / 1e6 if v is not None else None
        if name == "queue_depth":
            return self.metrics.gauge_last("queue_depth", t0, t1, service=service)
        raise ValueError(metric)


def holds(op: str, value: float, threshold: float) -> bool:
    return value < threshold if op == "<" else value > threshold

