"""The console's backend: launching runs, streaming trace events, charts, scorecard, resume, eval endpoints."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opspilot import evals
from opspilot.store import Store
from opspilot.web import create_app

pytestmark = pytest.mark.skipif(not evals.SNAPSHOTS.exists(), reason="no snapshots recorded")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "ui.db"))


def wait_run(client, rid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{rid}").json()
        if r["done"]:
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def start(client, **kw):
    body = {"scenario": "bad_deploy_coupon_exception", "agent": "heuristic", "mode": "replay", "approval": "auto", "pace": False, **kw}
    r = client.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_meta_lists_scenarios_and_gates_llm_agents_without_a_key(client, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPSPILOT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    m = client.get("/api/meta").json()
    assert len(m["scenarios"]) == 8 and all("ground_truth" not in s for s in m["scenarios"])
    by = {a["id"]: a for a in m["agents"]}
    assert by["heuristic"]["available"] and not by["llm"]["available"] and "ANTHROPIC_API_KEY" in by["llm"]["why"]
    assert client.post("/api/runs", json={"scenario": "bad_deploy_coupon_exception", "agent": "llm"}).status_code == 400


def test_auto_run_streams_trace_and_stores_charts_and_scorecard(client):
    run = wait_run(client, start(client))
    assert run["status"] == "resolved" and run["error"] is None
    d = client.get(f"/api/incidents/{run['incident_id']}").json()
    events = [e["event"] for e in d["audit"]]
    assert events.count("tool_call") >= 3 and "findings" in events
    assert events.index("approval_approved") < events.index("executed") < events.index("verification")
    call = next(e for e in d["audit"] if e["event"] == "tool_call")["payload"]
    assert call["evidence_id"].startswith("ev-") and call["tool"] and "result" in call
    assert d["charts"]["series"] and d["state"]["acted_at"] is not None
    assert d["scorecard"]["success"] and d["scorecard"]["rca_correct"] and not d["scorecard"]["wrong_action_executed"]
    assert d["chain_ok"] and "charts" not in d["state"]


def test_ground_truth_never_reaches_a_running_incident(client):
    rid = start(client, approval="web")
    for _ in range(100):
        r = client.get(f"/api/runs/{rid}").json()
        if r["incident_id"]:
            d = client.get(f"/api/incidents/{r['incident_id']}").json()
            if d["status"] == "awaiting_approval":
                break
        time.sleep(0.05)
    assert d["scorecard"] is None and d["approvals"][0]["status"] == "pending"
    assert client.post(f"/api/approvals/{d['approvals'][0]['id']}", json={"approve": False, "by": "t"}).status_code == 200
    run = wait_run(client, rid)
    assert run["status"] == "escalated"
    d = client.get(f"/api/incidents/{r['incident_id']}").json()
    assert not any(e["event"] == "executed" for e in d["audit"])


def test_wrong_fix_is_flagged_in_the_scorecard(client):
    run = wait_run(client, start(client, scenario="downstream_latency_red_herring_deploy"))
    sc = client.get(f"/api/incidents/{run['incident_id']}").json()["scorecard"]
    assert not sc["success"] and sc["wrong_action_executed"] and sc["blamed_red_herring"]


def test_failed_runner_marks_the_incident_failed_not_stuck(client, monkeypatch):
    from opspilot import incident
    monkeypatch.setattr(incident, "act", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run = wait_run(client, start(client))
    assert "boom" in run["error"]
    assert client.get(f"/api/incidents/{run['incident_id']}").json()["status"] == "failed"


def test_resume_endpoint_reattaches_a_runner_after_a_restart(tmp_path):
    db = tmp_path / "r.db"
    c1 = TestClient(create_app(db))
    rid = start(c1, approval="web")
    for _ in range(100):
        r = c1.get(f"/api/runs/{rid}").json()
        if r["incident_id"] and c1.get(f"/api/incidents/{r['incident_id']}").json()["status"] == "awaiting_approval":
            break
        time.sleep(0.05)
    iid = r["incident_id"]
    # a second app instance on the same db models a restarted server: no runner is attached to the incident
    c2 = TestClient(create_app(db))
    d = c2.get(f"/api/incidents/{iid}").json()
    assert d["resumable"]
    assert c2.post(f"/api/incidents/{iid}/resume").status_code == 200
    aid = d["approvals"][0]["id"]
    assert c2.post(f"/api/approvals/{aid}", json={"approve": True, "by": "alice"}).status_code in (200, 409)
    for _ in range(100):
        if Store(db).get(iid)["status"] == "resolved":
            break
        time.sleep(0.1)
    assert Store(db).get(iid)["status"] == "resolved"
    assert len(Store(db).approvals_for(iid)) == 1


def test_eval_endpoint_runs_and_reports_aggregate(client, tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "RESULTS", tmp_path / "results")
    assert client.post("/api/eval", json={"agents": ["null", "oracle"], "runs": 1}).status_code == 200
    for _ in range(200):
        d = client.get("/api/eval").json()
        if not d["job"]["running"] and d["aggregate"]:
            break
        time.sleep(0.1)
    assert d["job"]["error"] is None and d["aggregate"]["oracle"]["success"] == 1.0 and d["aggregate"]["null"]["success"] == 0.0
    assert d["aggregate"]["oracle"]["unapproved_exec"] == 0 and len(d["scenarios"]) == 8
