"""Run a scenario against the local testbed and freeze its telemetry as a snapshot.

Snapshot layout (data/snapshots/<scenario>/<branch>/):
  meta.json      scenario id, branch, recording window         (visible to the agent)
  alert.json     the page that starts the investigation        (visible to the agent)
  changes.jsonl  deploy / config / flag history                (visible to the agent)
  logs.jsonl     merged structured logs from all services      (visible to the agent)
  metrics.jsonl  scraped Prometheus samples, 2s resolution     (visible to the agent)
  harness.json   fault start, fix time: HARNESS ONLY, never exposed through agent tools

Two branches per scenario that has a safe fix: `nofix` (fault just keeps going) and `fix`
(correct action applied at fix_at_s). The replay environment serves whichever branch matches
what the agent actually did, which is how recovery verification gets evaluated offline.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
from prometheus_client.parser import text_string_to_metric_families

from opspilot import injector
from opspilot.scenarios import load
from opspilot.schemas import Scenario
from opspilot.stack import LocalStack
from testbed.common.state import read_jsonl
from testbed.deployctl import init_state

KEEP_PREFIXES = ("http_", "db_", "process_", "payments_", "queue_", "jobs_")


class Scraper(threading.Thread):
    def __init__(self, targets: dict[str, str], out: Path, interval: float = 2.0):
        super().__init__(daemon=True)
        self.targets, self.out, self.interval = targets, out, interval
        self._halt = threading.Event()
        self.rows = 0

    def run(self) -> None:
        with httpx.Client(timeout=3) as client, open(self.out, "w") as f:
            while not self._halt.is_set():
                t0 = time.time()
                for service, url in self.targets.items():
                    try:
                        text = client.get(url).text
                    except httpx.HTTPError:
                        continue  # a restarting service simply has a gap in its series
                    for fam in text_string_to_metric_families(text):
                        for s in fam.samples:
                            if s.name.startswith(KEEP_PREFIXES) and not s.name.endswith("_created") and s.value == s.value:
                                f.write(json.dumps({"ts": round(t0, 3), "service": service, "name": s.name,
                                                    "labels": s.labels, "value": s.value}) + "\n")
                                self.rows += 1
                f.flush()
                self._halt.wait(max(0.0, self.interval - (time.time() - t0)))

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)


def record(scenario_id: str, branch: str, out_root: Path, speed: float = 1.0,
           log: Callable[[str], None] = print) -> Path:
    sc: Scenario = load(scenario_id)
    if branch not in ("nofix", "fix"):
        raise ValueError(branch)
    if branch == "fix" and not sc.has_fix_branch:
        raise ValueError(f"{scenario_id} has no safe automated fix; only 'nofix' exists")
    tl = sc.timeline.scaled(speed)
    out = Path(out_root) / scenario_id / branch
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    work = Path(tempfile.mkdtemp(prefix=f"opspilot-{scenario_id}-"))
    state = work / "state"
    init_state(state)  # before any process starts, so services never race on first-time setup
    stack = LocalStack(state, work / "proc")
    scraper = Scraper(stack.metrics_targets, out / "metrics.jsonl")
    try:
        t0 = time.time()
        stack.start()
        scraper.start()
        # Anchor the timeline once services are up, so every scenario gets a full baseline.
        t_fault = time.time() + tl.baseline_s
        events: list[tuple[float, Callable[[], None]]] = [
            (t_fault + i.at, (lambda i=i: injector.inject(i, state))) for i in sc.inject]
        if branch == "fix":
            t_fix = t_fault + tl.fix_at_s
            for a in sc.ground_truth.correct_actions:
                events.append((t_fix, (lambda a=a: injector.execute_action(a, state, stack))))
            t_end = t_fix + tl.recovery_s
        else:
            t_fix = None
            t_end = t_fault + tl.fault_s
        for when, fn in sorted(events, key=lambda e: e[0]):
            time.sleep(max(0.0, when - time.time()))
            fn()
        time.sleep(max(0.0, t_end - time.time()))
        t_stop = time.time()
    finally:
        scraper.stop()
        stack.stop()

    logs = []
    for p in sorted((state / "logs").glob("*.jsonl")):
        logs += read_jsonl(p)
    logs.sort(key=lambda r: r["ts"])
    (out / "logs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs))
    shutil.copy(state / "changes.jsonl", out / "changes.jsonl")
    alert = sc.alert.model_copy(update={"fired_at": round(t_fault + tl.alert_delay_s, 3)})
    (out / "alert.json").write_text(alert.model_dump_json(indent=2))
    (out / "meta.json").write_text(json.dumps(
        {"scenario_id": sc.id, "branch": branch, "start_ts": round(t0, 3), "end_ts": round(t_stop, 3),
         "scrape_interval_s": scraper.interval}, indent=2))
    (out / "harness.json").write_text(json.dumps(
        {"fault_start_ts": round(t_fault, 3), "fix_applied_ts": round(t_fix, 3) if t_fix else None,
         "baseline_s": tl.baseline_s}, indent=2))
    shutil.rmtree(work, ignore_errors=True)
    log(f"recorded {scenario_id}/{branch}: {len(logs)} log lines, {scraper.rows} metric samples")
    return out
