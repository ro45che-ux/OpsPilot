"""Integration tests against the committed benchmark snapshots (replay) and, marked slow, the live testbed."""
from __future__ import annotations

import json

import pytest

from opspilot import evals, scenarios
from opspilot.env import ReplayEnv
from opspilot.incident import AutoApprover, run_incident
from opspilot.schemas import Action
from opspilot.store import Store
from opspilot.validate import validate_scenario

ROOT = evals.SNAPSHOTS
IDS = list(scenarios.load_all())
pytestmark = pytest.mark.skipif(not ROOT.exists(), reason="no snapshots recorded")


@pytest.mark.parametrize("sid", IDS)
def test_snapshot_shows_the_symptom_its_ground_truth_claims(sid):
    problems, _ = validate_scenario(sid, ROOT)
    assert problems == []


def test_grader_is_correct_oracle_passes_everything_and_wrong_oracle_nothing():
    rows = {a: [evals.run_episode(a, s) for s in IDS] for a in ("oracle", "oracle-wrong", "null")}
    assert all(r["success"] and r["verify_correct"] in (True, None) for r in rows["oracle"])  # None: escalated, nothing ran
    assert [r["verify_correct"] for r in rows["oracle"]].count(None) == 2
    assert not any(r["success"] for r in rows["oracle-wrong"])
    assert all(r["wrong_executed"] for r in rows["oracle-wrong"])
    assert not any(r["success"] for r in rows["null"])
    # a wrong fix that actually ran must be caught by the verifier, not reported as recovered
    assert all(r["verdict"] in ("not_recovered", "regressed") for r in rows["oracle-wrong"])


def test_no_agent_ever_executes_without_approval():
    for a in ("null", "heuristic", "oracle", "oracle-wrong"):
        assert all(evals.run_episode(a, s)["unapproved_exec"] == 0 for s in IDS)


def test_runbook_baseline_is_fooled_by_the_red_herring_and_only_by_it():
    res = {s: evals.run_episode("heuristic", s) for s in IDS}
    rh = res["downstream_latency_red_herring_deploy"]
    assert rh["red_herring"] and rh["wrong_executed"] and not rh["success"]
    assert rh["status"] == "escalated" and rh["verdict"] != "recovered"
    assert [s for s, r in res.items() if not r["success"]] == ["downstream_latency_red_herring_deploy"]


def test_replay_is_deterministic():
    a = evals.run_episode("heuristic", "bad_deploy_coupon_exception")
    b = evals.run_episode("heuristic", "bad_deploy_coupon_exception")
    keep = ("success", "status", "verdict", "top_hypothesis", "proposals", "tool_calls")
    assert {k: a[k] for k in keep} == {k: b[k] for k in keep}


@pytest.mark.parametrize("sid", IDS)
def test_agent_can_never_see_the_future_or_ground_truth(sid):
    env = ReplayEnv(sid, ROOT)
    for tool, args in [("find_anomalies", {}), ("correlate_changes", {}), ("log_patterns", {}), ("search_logs", {"limit": 40}),
                       ("list_changes", {}), ("query_series", {"series": "api.p99_latency_s"})]:
        text = json.dumps(env.call(tool, args))
        assert "fault_start" not in text and "harness" not in text and "ground_truth" not in text
    assert max(r["ts"] for r in env.tel.logs) <= env.t_inv
    assert max(r["ts"] for r in env.tel.metric_rows) <= env.t_inv
    assert all(a["ago_s"] >= 0 for a in env.call("list_changes")["result"]["changes"])


def test_acting_changes_the_world_only_when_the_action_is_right():
    sid = "bad_deploy_n_plus_one"
    wrong, right = ReplayEnv(sid, ROOT), ReplayEnv(sid, ROOT)
    wrong.execute(Action(type="restart_service", service="api"))
    right.execute(Action(type="rollback_deploy", service="api", to_version="1.4.1"))
    w, r = wrong.watch(), right.watch()
    assert w.series("api.p99_latency_s")[-1][1] > 0.5 > r.series("api.p99_latency_s")[-1][1]
    assert any(c.rollback for c in r.changes) and not any(c.rollback for c in w.changes)


def test_wrong_rollback_target_is_denied_by_policy_before_it_can_run():
    env = ReplayEnv("bad_deploy_n_plus_one", ROOT)
    cfg = env.config_of("api")
    assert cfg["version"] == "1.5.0" and "1.4.1" in cfg["known_versions"]
    from opspilot import policy
    assert policy.evaluate(Action(type="rollback_deploy", service="api", to_version="1.2.0"), env.config_of).verdict == "deny"


def test_revert_value_comes_from_history_so_invented_values_are_denied():
    env = ReplayEnv("config_db_pool_too_small", ROOT)
    from opspilot import policy
    assert env.config_of("api")["config"]["db_pool_size"] == 2
    ok = policy.evaluate(Action(type="revert_config", service="api", key="db_pool_size", value=8), env.config_of)
    bad = policy.evaluate(Action(type="revert_config", service="api", key="db_pool_size", value=64), env.config_of)
    assert ok.verdict == "needs_approval" and bad.verdict == "deny"


def test_basic_mode_gives_the_same_world_with_fewer_tools():
    full, basic = ReplayEnv("bad_deploy_n_plus_one", ROOT, "full"), ReplayEnv("bad_deploy_n_plus_one", ROOT, "basic")
    assert len(basic.tools()) < len(full.tools())
    assert basic.call("list_changes")["result"] == full.call("list_changes")["result"]


@pytest.mark.slow
def test_live_end_to_end_rollback_on_the_real_testbed():
    from opspilot.agents.heuristic import HeuristicAgent
    from opspilot.live import run_live
    store = Store()
    out = run_live("bad_deploy_n_plus_one", HeuristicAgent(), store, AutoApprover(), log=lambda m: None)
    assert out.status == "resolved" and out.verification["verdict"] == "recovered"
    assert [a.type for a in out.executed] == ["rollback_deploy"]
    ev = [e["event"] for e in store.audit_log(out.incident_id)]
    assert ev.index("approval_approved") < ev.index("executed") < ev.index("verification")
