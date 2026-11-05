"""Incident lifecycle: investigate -> propose -> policy -> human approval -> execute -> verify.

State lives in the Store (SQLite) so an incident can wait hours for a human and survive a restart.
Invariants enforced here and checked in tests:
  * nothing mutating runs without an approval row whose status is 'approved' (re-read right before executing)
  * success criteria are written to the audit log BEFORE the action runs
  * a rejected, denied or unverified fix ends in escalation to a human, never in a silent retry loop
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

from opspilot import policy, telemetry as T
from opspilot.agents.base import Agent
from opspilot.agents.heuristic import criteria_from_anomalies
from opspilot.env import WATCH_S, Env
from opspilot.findings import Findings
from opspilot.schemas import Action, Criterion
from opspilot.store import Store
from opspilot.verify import snapshot_values, verify


class Approver(Protocol):
    def decide(self, store: Store, approval_id: str, card: dict) -> tuple[bool, str, str]:
        """Returns (approved, decided_by, reason)."""


class AutoApprover:
    """Rubber-stamps whatever policy let through: the worst-case human, used by evals."""
    def decide(self, store, approval_id, card):
        return True, "auto-approver", "eval: approve everything policy allows"


class RejectApprover:
    def decide(self, store, approval_id, card):
        return False, "auto-rejector", "rejected"


class CLIApprover:
    def decide(self, store, approval_id, card):
        a = card["action"]
        print("\n" + "=" * 70)
        print(f"APPROVAL NEEDED  [{approval_id}]  incident {card['incident_id']}")
        print(f"  root cause : {card['root_cause']}")
        print(f"  evidence   : {', '.join(card['evidence_ids'])}")
        print(f"  action     : {a['type']} {({k: v for k, v in a.items() if k != 'type' and v is not None})}")
        print(f"  plan       : {card['plan']}")
        r = card["risk"]
        print(f"  risk       : {r['level']} | blast radius: {r['blast_radius']} | reversible: {r['reversible']}")
        print(f"  recovery   : " + "; ".join(f"{c['metric']} {c['op']} {c['value']}" for c in card["criteria"]))
        print("=" * 70)
        ans = input("Approve? [y/N] ").strip().lower() if sys.stdin.isatty() else "n"
        return ans in ("y", "yes"), "cli-user", "interactive"


class StoreApprover:
    """Waits for the decision to be written to the DB by another process (the web UI)."""
    def __init__(self, timeout_s: float = 3600, poll_s: float = 0.5):
        self.timeout_s, self.poll_s = timeout_s, poll_s

    def decide(self, store, approval_id, card):
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            a = store.approval(approval_id)
            if a["status"] != "pending":
                return a["status"] == "approved", a["decided_by"] or "web", a["reason"] or ""
            time.sleep(self.poll_s)
        return False, "timeout", "no decision before timeout"


@dataclass
class Outcome:
    incident_id: str
    status: str
    findings: Findings | None = None
    policy: list[dict] = field(default_factory=list)       # one entry per proposal considered
    executed: list[Action] = field(default_factory=list)
    verification: dict | None = None
    reason: str = ""
    agent_stats: dict = field(default_factory=dict)


def _preview(result, limit: int = 6000):
    s = json.dumps(result, default=str)
    return result if len(s) <= limit else {"truncated": True, "text": s[:limit]}


def _save_charts(store: Store, iid: str, tel: T.Telemetry, **extra) -> None:
    try:  # charts are decoration for the UI; they must never be able to break an incident
        store.update_state(iid, charts=T.chart_data(tel), **extra)
    except Exception:  # noqa: BLE001
        pass


def run_incident(env: Env, agent: Agent, store: Store, approver: Approver, scenario: str | None = None,
                 watch_s: float = WATCH_S, on_start=None) -> Outcome:
    iid = store.create_incident(scenario)
    if on_start:
        on_start(iid)
    env.on_call = lambda eid, tool, args, result: store.audit(
        iid, "tool_call", agent.name, {"evidence_id": eid, "tool": tool, "args": args, "result": _preview(result)})
    env.on_note = lambda text: store.audit(iid, "agent_note", agent.name, {"text": text[:1500]})
    store.audit(iid, "alert_received", "system", env.alert_summary())
    store.set_status(iid, "investigating")
    res = agent.run(env)
    _save_charts(store, iid, env.tel)
    store.audit(iid, "agent_finished", agent.name, {"stats": res.stats, "error": res.error,
                                                    "evidence": sorted(env.evidence)})
    out = Outcome(iid, "investigating", res.findings, agent_stats=res.stats)
    if res.findings is None:
        return _escalate(store, out, f"agent produced no valid findings: {res.error}", from_status="investigating")
    store.update_state(iid, findings=res.findings.model_dump(mode="json"), scenario=scenario,
                       evidence={k: {"tool": v.tool, "args": v.args} for k, v in env.evidence.items()})
    store.audit(iid, "findings", agent.name, res.findings.model_dump(mode="json"))
    store.set_status(iid, "proposed")
    return act(env, store, approver, iid, out, watch_s)


def act(env: Env, store: Store, approver: Approver, iid: str, out: Outcome, watch_s: float = WATCH_S) -> Outcome:
    f = out.findings or Findings.model_validate(store.get(iid)["state"]["findings"])
    out.findings = f
    top = f.hypotheses[0]
    criteria = list(f.success_criteria) or criteria_from_anomalies(
        [{"series": a.series, "baseline": a.baseline, "current": a.current} for a in T.detect_anomalies(env.tel)])
    mutations = 0
    for prop in f.proposals:
        d = policy.evaluate(prop.action, env.config_of, mutations)
        out.policy.append({"action": prop.action.model_dump(mode="json", exclude_none=True), "verdict": d.verdict, "reasons": d.reasons})
        store.audit(iid, "policy_decision", "policy", out.policy[-1])
        if d.verdict == "deny":
            continue
        if prop.action.type == "escalate":
            return _escalate(store, out, prop.action.reason or "agent chose to escalate",
                             from_status=store.get(iid)["status"])

        status = store.get(iid)["status"]
        if status == "proposed":
            store.set_status(iid, "awaiting_approval")
        elif status == "verifying":
            store.set_status(iid, "awaiting_approval")
        store.audit(iid, "success_criteria_declared", "system", {"criteria": [c.model_dump() for c in criteria]})
        a_json = prop.action.model_dump(mode="json", exclude_none=True)
        existing = store.get(iid)["state"].get("pending_approval")
        aid = existing or store.request_approval(iid, a_json, d.risk.model_dump(), d.plan)
        store.update_state(iid, pending_approval=aid)
        card = {"incident_id": iid, "action": a_json, "plan": d.plan, "risk": d.risk.model_dump(),
                "root_cause": top.summary, "evidence_ids": top.evidence_ids, "criteria": [c.model_dump() for c in criteria]}
        approved, by, why = approver.decide(store, aid, card)
        if store.approval(aid)["status"] == "pending":
            store.decide(aid, approved, by, why)
        store.update_state(iid, pending_approval=None)
        if store.approval(aid)["status"] != "approved":
            return _escalate(store, out, f"human rejected the action ({why})", from_status="awaiting_approval")

        before = snapshot_values(env.tel, criteria)
        acted_at = env.tel.now
        store.set_status(iid, "executing", actor=by)
        assert store.approval(aid)["status"] == "approved", "refusing to execute without an approved approval"
        env.execute(prop.action)
        mutations += 1
        out.executed.append(prop.action)
        store.audit(iid, "executed", "executor", {"action": a_json, "approval_id": aid})
        store.set_status(iid, "verifying")
        after = env.watch(watch_s)
        out.verification = verify(after, criteria, before)
        _save_charts(store, iid, after, acted_at=acted_at)
        store.audit(iid, "verification", "verifier", out.verification)
        if out.verification["verdict"] == "recovered":
            store.set_status(iid, "resolved", reason="recovery criteria met")
            out.status, out.reason = "resolved", "recovery criteria met"
            return out
        # not recovered: fall through to the next proposal (needs its own policy check and approval)
    return _escalate(store, out, "no approved action recovered the system" if out.executed
                     else "no proposal was both allowed by policy and acted on", from_status=store.get(iid)["status"])


def _escalate(store: Store, out: Outcome, reason: str, from_status: str) -> Outcome:
    st = store.get(out.incident_id)["status"]
    if st not in ("escalated", "resolved", "failed"):
        store.set_status(out.incident_id, "escalated", reason=reason)
    out.status, out.reason = "escalated", reason
    return out


def resume(env: Env, store: Store, approver: Approver, iid: str, watch_s: float = WATCH_S) -> Outcome:
    inc = store.get(iid)
    if inc["status"] not in ("proposed", "awaiting_approval"):
        raise ValueError(f"{iid} is {inc['status']}; only proposed/awaiting_approval incidents can be resumed")
    store.audit(iid, "resumed", "system", {"from": inc["status"]})
    return act(env, store, approver, iid, Outcome(iid, inc["status"]), watch_s)
