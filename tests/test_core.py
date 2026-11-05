"""Policy, store, telemetry analysis, findings validation, agent loop and incident invariants."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opspilot import evals, policy, report, telemetry as T
from opspilot.agents.heuristic import HeuristicAgent
from opspilot.agents.llm import Block, LLMAgent, LLMResponse
from opspilot.env import Evidence
from opspilot.findings import Findings, Hypothesis, Proposal, validate_findings
from opspilot.incident import AutoApprover, RejectApprover, StoreApprover, resume, run_incident
from opspilot.schemas import Action, Criterion, Matcher
from opspilot.store import IllegalTransition, Store
from opspilot.web import create_app
from tests.helpers import StubEnv, change, tel


# ---- policy ----------------------------------------------------------------------------------
def cfg(service):
    return {"service": service, "version": "1.5.0", "known_versions": ["1.4.1", "1.4.0"],
            "config": {"db_pool_size": 2, "flags.new_pricing_engine": True},
            "config_history": {"db_pool_size": [{"change_id": "c", "old": 8, "new": 2, "ago_s": 5}],
                               "flags.new_pricing_engine": [{"change_id": "f", "old": False, "new": True, "ago_s": 5}]}}


def ev(a: Action, n=0):
    return policy.evaluate(a, cfg, n)


def test_every_mutation_needs_approval_and_escalate_is_free():
    ok = ev(Action(type="rollback_deploy", service="api", to_version="1.4.1"))
    assert ok.verdict == "needs_approval" and ok.risk and "1.5.0 -> 1.4.1" in ok.plan
    assert ev(Action(type="restart_service", service="worker")).verdict == "needs_approval"
    assert ev(Action(type="revert_config", service="api", key="db_pool_size", value=8)).verdict == "needs_approval"
    assert ev(Action(type="escalate", reason="third party")).verdict == "allow"


@pytest.mark.parametrize("action,why", [
    (Action(type="rollback_deploy", service="api", to_version="0.9.0"), "never deployed"),
    (Action(type="rollback_deploy", service="api", to_version="1.5.0"), "already on"),
    (Action(type="rollback_deploy", service="api"), "needs to_version"),
    (Action(type="revert_config", service="api", key="db_pool_size", value=500), "not a value"),
    (Action(type="revert_config", service="api", key="secret_key", value=1), "not revertable"),
    (Action(type="revert_config", service="api", key="db_pool_size"), "needs key and value"),
    (Action(type="escalate"), "needs a reason"),
])
def test_policy_denies_unsafe_or_malformed_actions(action, why):
    d = ev(action)
    assert d.verdict == "deny" and why in " ".join(d.reasons)


def test_rollforward_and_mutation_budget_denied():
    c = lambda s: {**cfg(s), "known_versions": ["1.9.9"]}  # noqa: E731
    assert "roll-forward" in " ".join(policy.evaluate(Action(type="rollback_deploy", service="api", to_version="1.9.9"), c).reasons)
    d = ev(Action(type="restart_service", service="api"), n=policy.MAX_MUTATIONS)
    assert d.verdict == "deny" and "budget" in d.reasons[0]


def test_flag_revert_allowed_for_any_flag_with_history():
    assert ev(Action(type="revert_config", service="api", key="flags.new_pricing_engine", value=False)).verdict == "needs_approval"


# ---- store -----------------------------------------------------------------------------------
def test_illegal_transitions_rejected():
    s = Store()
    iid = s.create_incident("x")
    with pytest.raises(IllegalTransition):
        s.set_status(iid, "executing")
    s.set_status(iid, "investigating")
    s.set_status(iid, "escalated")
    with pytest.raises(IllegalTransition):
        s.set_status(iid, "investigating")


def test_audit_chain_detects_tampering():
    s = Store()
    iid = s.create_incident("x")
    s.audit(iid, "a", "t", {"k": 1})
    s.audit(iid, "b", "t", {"k": 2})
    assert s.verify_chain() == (True, None)
    s.db.execute("UPDATE audit SET payload='{\"k\": 99}' WHERE event='a'")
    ok, bad = s.verify_chain()
    assert not ok and bad is not None


def test_approval_can_only_be_decided_once():
    s = Store()
    iid = s.create_incident("x")
    aid = s.request_approval(iid, {"type": "restart_service"}, {}, "plan")
    s.decide(aid, True, "me")
    with pytest.raises(ValueError):
        s.decide(aid, False, "someone else")


# ---- telemetry -------------------------------------------------------------------------------
def test_template_clustering_collapses_ids_numbers_and_quotes():
    a = {"level": "ERROR", "service": "api", "msg": "x", "route": "r", "status": 500, "error": "KeyError: 'SUMMER23'"}
    b = {**a, "error": "KeyError: 'BLACKFRIDAY22'"}
    c = {**a, "error": "PoolTimeout: timed out after 1.0s (pool_size=2, in_use=2)"}
    assert T.template_of(a) == T.template_of(b) != T.template_of(c)
    assert "<*>" in T.template_of(c) and "1.0" not in T.template_of(c)


def test_anomaly_detected_with_onset_and_ignored_when_flat():
    t = tel(degraded_from=84.0)
    an = T.detect_anomalies(t)
    assert [a.series for a in an] == ["worker.queue_depth"]
    assert 80 <= an[0].onset_ts <= 90 and an[0].baseline == 0 and an[0].current == 100
    assert T.detect_anomalies(tel(degraded_from=None)) == []


def test_correlation_prefers_recent_change_on_affected_service_and_ignores_old_ones():
    near = change("near", ts=80.0, service="worker")
    other_service = change("other", ts=82.0, service="api")
    old = change("old", ts=-5000.0, service="worker")
    t = tel(changes=[near, other_service, old])
    cands = T.correlate_changes(t, T.detect_anomalies(t))
    assert [c.change.id for c in cands] == ["near", "other"]
    assert cands[0].score > cands[1].score and cands[0].same_service_anomaly


def test_log_patterns_flag_new_and_field_shift():
    logs = [{"ts": float(i), "level": "INFO", "service": "api", "msg": "request", "route": "r", "status": 200,
             "db_queries": 2 if i < 40 else 30} for i in range(0, 120)]
    logs += [{"ts": 100.0 + i, "level": "ERROR", "service": "api", "msg": "boom", "error": f"E{i}"} for i in range(5)]
    pats = T.log_patterns(tel(logs=logs))
    by = {p["template"].split(" | ")[2]: p for p in pats}
    assert by["boom"]["trend"] == "new"
    assert any("db_queries" in s for s in by["request"].get("field_shift", []))


# ---- findings validation ---------------------------------------------------------------------
def _h(**kw):
    base = dict(category="poison_message", service="worker", summary="s", confidence=0.5, evidence_ids=["ev-001"])
    return Hypothesis(**{**base, **kw})


def _f(h=None, action=None):
    return Findings(summary="s", hypotheses=[h or _h()], success_criteria=[],
                    proposals=[Proposal(action=action or Action(type="restart_service", service="worker"), rationale="r")])


def test_validator_rejects_fabricated_failed_and_missing_evidence():
    env = StubEnv()
    env.evidence["ev-001"] = Evidence("ev-001", "find_anomalies", {}, {"anomalies": []})
    env.evidence["ev-002"] = Evidence("ev-002", "get_change_diff", {}, {"error": "no change"})
    assert validate_findings(_f(), env) == []
    assert any("does not exist" in e for e in validate_findings(_f(_h(evidence_ids=["ev-999"])), env))
    assert any("failed tool call" in e for e in validate_findings(_f(_h(evidence_ids=["ev-002"])), env))
    assert any("cite at least one" in e for e in validate_findings(_f(_h(evidence_ids=[])), env))


def test_validator_requires_real_change_for_change_categories():
    env = StubEnv()
    env.evidence["ev-001"] = Evidence("ev-001", "x", {}, {})
    assert any("must name" in e for e in validate_findings(_f(_h(category="bad_deploy", service="api")), env))
    assert any("does not exist" in e for e in validate_findings(_f(_h(category="bad_deploy", service="api", change_id="nope")), env))
    assert validate_findings(_f(_h(category="bad_deploy", service="api", change_id="chg-x")), env) == []


def test_validator_checks_action_parameters():
    env = StubEnv()
    env.evidence["ev-001"] = Evidence("ev-001", "x", {}, {})
    assert any("missing 'to_version'" in e for e in validate_findings(_f(action=Action(type="rollback_deploy", service="api")), env))


# ---- LLM agent loop (scripted model, no network) --------------------------------------------
class ScriptedClient:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        blocks = self.turns.pop(0)
        return LLMResponse(blocks, input_tokens=100, output_tokens=20)


def use(name, inp, i=[0]):
    i[0] += 1
    return Block("tool_use", id=f"tu{i[0]}", name=name, input=inp)


GOOD = {"summary": "queue blocked", "hypotheses": [{"category": "poison_message", "service": "worker", "summary": "bad job",
        "confidence": 0.8, "evidence_ids": ["ev-001"]}], "proposals": [{"action": {"type": "restart_service", "service": "worker"},
        "rationale": "quarantine"}], "success_criteria": [{"metric": "worker.queue_depth", "op": "<", "value": 50}]}


def test_llm_agent_happy_path_counts_tool_calls_and_tokens():
    c = ScriptedClient([[use("find_anomalies", {})], [use("submit_findings", GOOD)]])
    r = LLMAgent(c).run(StubEnv())
    assert r.error is None and r.findings.hypotheses[0].category == "poison_message"
    assert r.stats["tool_calls"] == 1 and r.stats["turns"] == 2 and r.stats["input_tokens"] == 200
    assert {t["name"] for t in c.calls[0]["tools"]} >= {"find_anomalies", "submit_findings"}


def test_llm_agent_gets_validation_errors_back_and_can_fix():
    bad = {**GOOD, "hypotheses": [{**GOOD["hypotheses"][0], "evidence_ids": ["ev-777"]}]}
    c = ScriptedClient([[use("find_anomalies", {})], [use("submit_findings", bad)], [use("submit_findings", GOOD)]])
    r = LLMAgent(c).run(StubEnv())
    assert r.error is None and r.stats["validation_retries"] == 1
    msg = c.calls[2]["messages"][-1]["content"][0]
    assert msg["is_error"] and "ev-777" in msg["content"]


def test_llm_agent_fails_after_repeated_fabrication():
    bad = {**GOOD, "hypotheses": [{**GOOD["hypotheses"][0], "evidence_ids": ["ev-777"]}]}
    c = ScriptedClient([[use("submit_findings", bad)]] * 3)
    r = LLMAgent(c, max_validation_retries=2).run(StubEnv())
    assert r.findings is None and "validation" in r.error


def test_llm_agent_budget_exhaustion_forces_submission():
    c = ScriptedClient([[use("find_anomalies", {})], [use("find_anomalies", {})], [use("find_anomalies", {})],
                        [use("submit_findings", GOOD)]])
    r = LLMAgent(c, max_tool_calls=2).run(StubEnv())
    assert r.stats["tool_calls"] == 2 and r.findings is not None
    assert c.calls[-1]["force_tool"] == "submit_findings"
    assert "budget" in c.calls[3]["messages"][-1]["content"][0]["content"]


def test_basic_mode_hides_analysis_tools():
    names = {s.name for s in StubEnv(mode="basic").tools()}
    assert "find_anomalies" not in names and "correlate_changes" not in names and "log_patterns" not in names
    assert {"search_logs", "query_metric_raw", "list_changes"} <= names


# ---- incident lifecycle invariants -----------------------------------------------------------
def run(env=None, approver=None, agent=None):
    env = env or StubEnv()
    store = Store()
    out = run_incident(env, agent or HeuristicAgent(), store, approver or AutoApprover(), "stub")
    return env, store, out


def events(store, out):
    return [e["event"] for e in store.audit_log(out.incident_id)]


def test_happy_path_resolves_and_approval_precedes_execution():
    env, store, out = run()
    assert out.status == "resolved" and out.verification["verdict"] == "recovered"
    ev_ = events(store, out)
    assert ev_.index("approval_approved") < ev_.index("executed")
    assert ev_.index("success_criteria_declared") < ev_.index("executed")
    assert store.verify_chain()[0]


def test_rejected_action_never_executes_and_escalates():
    env, store, out = run(approver=RejectApprover())
    assert out.status == "escalated" and env.executed == [] and "executed" not in events(store, out)


def test_unrecovered_fix_is_escalated_not_reported_resolved():
    env, store, out = run(StubEnv(recovers=False))
    assert out.status == "escalated" and out.verification["verdict"] in ("not_recovered", "regressed")


def test_policy_denied_proposal_never_executes():
    class Rogue:
        name = "rogue"

        def run(self, env):
            from opspilot.agents.base import AgentResult
            a = env.call("find_anomalies")["evidence_id"]
            f = _f(_h(evidence_ids=[a]), Action(type="revert_config", service="api", key="db_pool_size", value=9999))
            return AgentResult(f, {})

    env, store, out = run(agent=Rogue())
    assert out.status == "escalated" and env.executed == [] and out.policy[0]["verdict"] == "deny"


def test_agent_failure_escalates():
    class Broken:
        name = "broken"

        def run(self, env):
            from opspilot.agents.base import AgentResult
            return AgentResult(None, {}, error="boom")

    _, _, out = run(agent=Broken())
    assert out.status == "escalated" and "boom" in out.reason


def test_resume_picks_up_awaiting_approval_after_a_crash():
    env = StubEnv()
    store = Store()

    class Crash(AutoApprover):
        def decide(self, store, aid, card):
            raise KeyboardInterrupt  # the process dies while a human is deciding

    with pytest.raises(KeyboardInterrupt):
        run_incident(env, HeuristicAgent(), store, Crash(), "stub")
    iid = store.list_incidents()[0]["id"]
    assert store.get(iid)["status"] == "awaiting_approval" and env.executed == []
    aid = store.get(iid)["state"]["pending_approval"]
    store.decide(aid, True, "alice")  # decided from the web UI while the runner was down
    out = resume(StubEnv(), store, StoreApprover(timeout_s=2, poll_s=0.01), iid)
    assert out.status == "resolved"
    assert len(store.approvals_for(iid)) == 1  # reused the pending approval, did not ask twice


def test_report_is_built_from_audit_log():
    _, store, out = run()
    md = report.render(store, out.incident_id)
    assert "resolved" in md and "approval_approved" in md and "audit chain intact: yes" in md


# ---- grading ---------------------------------------------------------------------------------
def test_evidence_supports_matchers_structurally():
    diff = Evidence("e", "get_change_diff", {"change_id": "chg-1"}, {"id": "chg-1", "diff": "x"})
    pats = Evidence("e2", "log_patterns", {}, {"patterns": [{"template": "ERROR | api | boom", "fields": {"attempt": {"recent": {"max": 7}}}}]})
    ser = Evidence("e3", "query_series", {"series": "api.p99_latency_s"}, {"points": []})
    assert evals.evidence_supports(diff, Matcher(source="change", ref="chg-1"))
    assert not evals.evidence_supports(diff, Matcher(source="change", ref="chg-2"))
    assert evals.evidence_supports(pats, Matcher(source="log", msg_contains="boom", service="api"))
    assert evals.evidence_supports(pats, Matcher(source="log", field="attempt", min=5))
    assert not evals.evidence_supports(pats, Matcher(source="log", field="attempt", min=9))
    assert evals.evidence_supports(ser, Matcher(source="metric", name="http_request_duration_seconds_bucket"))
    assert not evals.evidence_supports(Evidence("e4", "x", {}, {"error": "bad"}), Matcher(source="change", ref="chg-1"))


# ---- web -------------------------------------------------------------------------------------
def test_web_lists_incidents_and_approves_once(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    iid = store.create_incident("stub")
    aid = store.request_approval(iid, {"type": "restart_service", "service": "worker"}, {"level": "low"}, "Restart worker")
    client = TestClient(create_app(db))
    assert client.get("/api/incidents").json()[0]["id"] == iid
    assert client.get(f"/api/incidents/{iid}").json()["approvals"][0]["status"] == "pending"
    assert client.post(f"/api/approvals/{aid}", json={"approve": True, "by": "bob"}).status_code == 200
    assert client.post(f"/api/approvals/{aid}", json={"approve": False}).status_code == 409
    assert client.post("/api/approvals/apr-nope", json={"approve": True}).status_code == 404
    assert "OpsPilot" in client.get("/").text
    assert "Incident" in client.get(f"/api/incidents/{iid}/report").text


def test_derived_criteria_are_always_satisfiable_by_the_healthy_baseline():
    from opspilot.agents.heuristic import criteria_from_anomalies
    # spiked then recovered: current == baseline == 0 must not yield "error_rate < 0"
    c = criteria_from_anomalies([{"series": "api.error_rate", "baseline": 0.0, "current": 0.0},
                                 {"series": "api.p99_latency_s", "baseline": 0.245, "current": 0.95},
                                 {"series": "api.cpu_percent", "baseline": 30, "current": 90}])
    assert [x.metric for x in c] == ["api.error_rate", "api.p99_latency_s"]  # cpu is not a recovery metric
    assert all(x.value > b for x, b in zip(c, [0.0, 0.245]))
