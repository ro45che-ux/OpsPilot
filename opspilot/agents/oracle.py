"""OracleAgent reads the scenario's ground truth and answers perfectly.

It is NOT a product agent. It exists to prove the harness itself is right: if the grader, policy
gate, executor and verifier are correct, the oracle must score 100% on every scenario, and a
deliberately wrong oracle must score 0. A harness that cannot pass that test grades nothing.
"""
from __future__ import annotations

from opspilot.agents.base import AgentResult
from opspilot.findings import Findings, Hypothesis, Proposal


class OracleAgent:
    def __init__(self, wrong: bool = False):
        self.wrong = wrong
        self.name = "oracle-wrong" if wrong else "oracle"

    def run(self, env) -> AgentResult:
        gt = env.scenario.ground_truth
        an = env.call("find_anomalies")
        ev = [an["evidence_id"]]
        rc = gt.root_cause
        if rc.change_id:
            ev.append(env.call("get_change_diff", {"change_id": rc.change_id})["evidence_id"])
        for m in gt.required_evidence:
            if m.source == "log":
                ev.append(env.call("log_patterns", {"service": m.service} if m.service else {})["evidence_id"])
                break
        for m in gt.required_evidence:
            if m.source == "metric" and env.mode == "full":
                series = next((k for k, v in __import__("opspilot.telemetry", fromlist=["SERIES"]).SERIES.items()
                               if m.name in v[2]), None)
                if series:
                    ev.append(env.call("query_series", {"series": series})["evidence_id"])
                break
        actions = gt.wrong_actions[:1] if self.wrong else gt.correct_actions
        h = Hypothesis(category=rc.category, service=rc.service, summary=rc.summary, confidence=0.9,
                       evidence_ids=ev, change_id=rc.change_id)
        f = Findings(summary=rc.summary, hypotheses=[h],
                     proposals=[Proposal(action=a, rationale="ground truth") for a in actions],
                     success_criteria=list(gt.success_criteria))
        return AgentResult(f, {"tool_calls": len(env.evidence), "turns": 0})
