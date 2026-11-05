"""Runbook-style baselines that need no LLM.

HeuristicAgent encodes what a junior on-call's runbook says: "find what changed right before it
broke and undo it; known signatures get known fixes". It uses the same tools and evidence IDs as
the LLM agent. It is intentionally naive about causality: the newest change near the onset wins,
so a coincidental deploy fools it. That gap is what the LLM agent has to close.

NullAgent never diagnoses and always escalates: the floor every other agent must beat.
"""
from __future__ import annotations

import time

from opspilot.agents.base import AgentResult
from opspilot.findings import Findings, Hypothesis, Proposal
from opspilot.schemas import Action, Criterion
from opspilot.telemetry import SERIES

CRITERIA_SERIES = {"api.error_rate", "api.p99_latency_s", "api.memory_mb", "worker.queue_depth"}


def criteria_from_anomalies(anomalies: list[dict]) -> list[Criterion]:
    """Midpoint between healthy baseline and degraded level: recovery means getting back below it.
    The degraded level is at least 2 detection floors above baseline, so a series that spiked and
    already fell back (current ~ baseline) can never produce an unsatisfiable `< baseline` criterion."""
    out = []
    for a in anomalies:
        if a["series"] in CRITERIA_SERIES:
            floor = SERIES[a["series"]][3]
            degraded = max(a["current"], a["baseline"] + 2 * floor)
            out.append(Criterion(metric=a["series"], op="<", value=round(a["baseline"] + 0.5 * (degraded - a["baseline"]), 3)))
    return out


class HeuristicAgent:
    name = "heuristic"

    def run(self, env) -> AgentResult:
        t0 = time.time()
        an = env.call("find_anomalies")
        co = env.call("correlate_changes")
        lp = env.call("log_patterns")
        anomalies = an["result"].get("anomalies", [])
        series = {a["series"] for a in anomalies}
        cands = co["result"].get("candidates", [])
        patterns = lp["result"].get("patterns", [])
        criteria = criteria_from_anomalies(anomalies)

        def done(h: Hypothesis, action: Action, why: str) -> AgentResult:
            f = Findings(summary=h.summary, hypotheses=[h], proposals=[Proposal(action=action, rationale=why)],
                         success_criteria=criteria)
            return AgentResult(f, {"tool_calls": len(env.evidence), "wall_s": round(time.time() - t0, 2), "turns": 1})

        # 1. known signature: a failing job retried forever behind a growing queue
        if ({"worker.queue_depth", "worker.job_failures_per_s"} & series) and any("job failed" in p["template"] for p in patterns):
            return done(Hypothesis(category="poison_message", service="worker", confidence=0.7,
                                   summary="A job fails on every retry and blocks the queue",
                                   evidence_ids=[an["evidence_id"], lp["evidence_id"]]),
                        Action(type="restart_service", service="worker"),
                        "runbook: restart worker so jobs over max_attempts are quarantined")

        # 2. newest change that lines up with the first anomaly gets blamed
        if cands and cands[0]["lead_s"] <= 120 and cands[0]["service_has_anomaly"]:
            c = cands[0]
            diff = env.call("get_change_diff", {"change_id": c["id"]})
            cat = {"deploy": "bad_deploy", "config": "config_change", "flag": "flag_change"}[c["type"]]
            ev = [co["evidence_id"], diff["evidence_id"], an["evidence_id"]]
            action = (Action(type="rollback_deploy", service=c["service"], to_version=c["from_version"])
                      if c["type"] == "deploy" else
                      Action(type="revert_config", service=c["service"], key=c["key"], value=c["old"]))
            return done(Hypothesis(category=cat, service=c["service"], confidence=round(min(0.95, c["score"]), 2),
                                   summary=f"{c['summary']} ({c['id']}) preceded the first anomaly by {c['lead_s']}s",
                                   evidence_ids=ev, change_id=c["id"]),
                        action, "runbook: undo the change closest to onset")

        # 3. third-party latency
        if "api.payments_p99_s" in series:
            return done(Hypothesis(category="downstream_dependency", service="payments", confidence=0.7,
                                   summary="Payments provider latency rose; no local change explains it",
                                   evidence_ids=[an["evidence_id"], lp["evidence_id"]]),
                        Action(type="escalate", reason="payments provider is slow; nothing on our side to undo"),
                        "runbook: escalate third-party degradation")

        return done(Hypothesis(category="unknown", service=cands[0]["service"] if cands else "api", confidence=0.2,
                               summary="No runbook rule matched", evidence_ids=[an["evidence_id"]]),
                    Action(type="escalate", reason="no rule matched"), "nothing to try safely")


class NullAgent:
    name = "null"

    def run(self, env) -> AgentResult:
        an = env.call("find_anomalies")
        f = Findings(summary="No diagnosis attempted",
                     hypotheses=[Hypothesis(category="unknown", service="api", confidence=0.0, summary="not investigated",
                                            evidence_ids=[an["evidence_id"]])],
                     proposals=[Proposal(action=Action(type="escalate", reason="no automated diagnosis"), rationale="floor baseline")],
                     success_criteria=[])
        return AgentResult(f, {"tool_calls": 1, "turns": 0})
