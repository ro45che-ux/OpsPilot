"""End-to-end against the real running testbed: inject the fault, let the symptom develop, investigate
from live telemetry, execute the approved action on the live processes and verify real recovery."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from opspilot import injector
from opspilot.env import LiveEnv
from opspilot.incident import Approver, Outcome, run_incident
from opspilot.recorder import Scraper
from opspilot.scenarios import load
from opspilot.stack import LocalStack
from opspilot.store import Store
from testbed.deployctl import init_state


def run_live(scenario_id: str, agent, store: Store, approver: Approver, mode: str = "full",
             log: Callable[[str], None] = print, on_start=None, pace: float = 0.0) -> Outcome:
    sc = load(scenario_id)
    tl = sc.timeline
    work = Path(tempfile.mkdtemp(prefix=f"opspilot-live-{scenario_id}-"))
    state = work / "state"
    init_state(state)
    stack = LocalStack(state, work / "proc")
    scraper = Scraper(stack.metrics_targets, work / "metrics.jsonl")
    try:
        stack.start()
        scraper.start()
        t_fault = time.time() + tl.baseline_s
        log(f"[live] healthy baseline for {tl.baseline_s:.0f}s ...")
        for op in sorted(sc.inject, key=lambda o: o.at):
            time.sleep(max(0.0, t_fault + op.at - time.time()))
            injector.inject(op, state)
        log(f"[live] fault injected ({scenario_id}); letting the symptom develop ...")
        time.sleep(max(0.0, t_fault + tl.fix_at_s - time.time()))
        alert = sc.alert.model_copy(update={"fired_at": t_fault + tl.alert_delay_s})
        env = LiveEnv(stack, state, work / "metrics.jsonl", alert, mode)
        log(f"[live] alert fired: {alert.name}; handing over to {agent.name}")
        env.pace = pace
        out = run_incident(env, agent, store, approver, scenario_id, on_start=on_start)
        log(f"[live] incident {out.incident_id}: {out.status} ({out.reason})")
        return out
    finally:
        scraper.stop()
        stack.stop()
        shutil.rmtree(work, ignore_errors=True)
