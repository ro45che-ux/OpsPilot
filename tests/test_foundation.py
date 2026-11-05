"""Fast tests for the foundation: scenario contracts, deploy system, derived metrics, app behaviours."""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from opspilot import scenarios
from opspilot.schemas import Scenario
from opspilot.snapshot import MetricStore
from testbed import deployctl
from testbed.common.state import read_json, read_jsonl
from testbed.releases.catalog import RELEASES


# ---- scenarios -------------------------------------------------------------------------------
def test_scenarios_load_and_are_consistent():
    all_sc = scenarios.load_all()
    assert len(all_sc) == 8
    for sc in all_sc.values():
        assert scenarios.check_consistency(sc) == [], sc.id


def test_every_scenario_names_a_correct_action_and_a_wrong_one():
    for sc in scenarios.load_all().values():
        assert sc.ground_truth.correct_actions and sc.ground_truth.wrong_actions, sc.id
        assert not set(map(str, sc.ground_truth.correct_actions)) & set(map(str, sc.ground_truth.wrong_actions))


def test_escalate_scenarios_have_no_fix_branch():
    sc = scenarios.load("downstream_payments_latency")
    assert not sc.has_fix_branch
    assert scenarios.load("bad_deploy_n_plus_one").has_fix_branch


def test_consistency_catches_a_dangling_change_reference():
    sc = scenarios.load("bad_deploy_n_plus_one")
    broken = Scenario.model_validate({**sc.model_dump(), "inject": []})
    assert any("chg-n1" in p for p in scenarios.check_consistency(broken))


# ---- deploy system ---------------------------------------------------------------------------
def test_init_state_is_idempotent_and_seeds_history(tmp_path):
    deployctl.init_state(tmp_path)
    first = read_jsonl(tmp_path / "changes.jsonl")
    deployctl.init_state(tmp_path)
    assert read_jsonl(tmp_path / "changes.jsonl") == first
    assert read_json(tmp_path / "release.json")["api"]["version"] == "1.4.1"
    assert all(c["ts"] < __import__("time").time() for c in first)


def test_deploy_and_rollback_record_changes(tmp_path):
    deployctl.init_state(tmp_path)
    c1 = deployctl.deploy("api", "1.5.0", state=tmp_path, change_id="chg-x")
    assert c1["from_version"] == "1.4.1" and c1["diff"] == RELEASES[("api", "1.5.0")].diff
    assert read_json(tmp_path / "release.json")["api"]["behaviors"] == ["n_plus_one"]
    c2 = deployctl.deploy("api", "1.4.1", state=tmp_path, rollback=True)
    assert c2["rollback"] and c2["from_version"] == "1.5.0"
    assert read_json(tmp_path / "release.json")["api"]["behaviors"] == []


def test_config_and_flag_changes_are_typed_and_keep_old_value(tmp_path):
    deployctl.init_state(tmp_path)
    cfg = deployctl.set_config("api", "db_pool_size", 2, state=tmp_path)
    flag = deployctl.set_config("api", "flags.new_pricing_engine", True, state=tmp_path)
    assert (cfg["type"], cfg["old"], cfg["new"]) == ("config", 8, 2)
    assert (flag["type"], flag["old"], flag["new"]) == ("flag", False, True)
    assert "-db_pool_size: 8" in cfg["diff"] and "+db_pool_size: 2" in cfg["diff"]


def test_environment_faults_are_not_in_change_history(tmp_path):
    deployctl.init_state(tmp_path)
    before = read_jsonl(tmp_path / "changes.jsonl")
    deployctl.set_fault("payments_latency_ms", 1000, state=tmp_path)
    deployctl.enqueue_poison(state=tmp_path)
    assert read_jsonl(tmp_path / "changes.jsonl") == before


# ---- derived metrics -------------------------------------------------------------------------
def _row(ts, name, value, service="api", **labels):
    return {"ts": ts, "service": service, "name": name, "labels": labels, "value": value}


def test_error_rate_and_rate_math():
    rows = []
    for t in range(0, 11):
        rows.append(_row(t, "http_requests_total", 10.0 * t, route="a", status="200"))
        rows.append(_row(t, "http_requests_total", 1.0 * t, route="a", status="500"))
    store = MetricStore(rows)
    assert store.rate("http_requests_total", 0, 10, service="api") == pytest.approx(11.0)
    assert store.rate("http_requests_total", 0, 10, service="api", status="500") == pytest.approx(1.0)


def test_histogram_quantile_interpolates():
    rows = []
    for t in (0, 10):
        scale = 0 if t == 0 else 1
        for le, c in {"0.1": 50, "0.5": 90, "1": 100, "+Inf": 100}.items():
            rows.append(_row(t, "lat_bucket", c * scale, le=le))
    store = MetricStore(rows)
    assert store.histogram_quantile(0.5, "lat", 0, 10) == pytest.approx(0.1)
    assert store.histogram_quantile(0.95, "lat", 0, 10) == pytest.approx(0.5 + 0.5 * (95 - 90) / 10)


# ---- api behaviours (in-process; no sockets, no sleeps beyond simulated RTT) ------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSPILOT_STATE_DIR", str(tmp_path))
    import testbed.api.app as app_mod
    app_mod = importlib.reload(app_mod)
    with TestClient(app_mod.app) as client:
        yield app_mod, client, tmp_path


def _last_request_log(state, route):
    lines = [json.loads(l) for l in (state / "logs" / "api.jsonl").read_text().splitlines()]
    return [r for r in lines if r.get("route") == route and r["msg"] != "deprecated header X-Legacy-Client present"][-1]


ORDER = {"customer": "c1", "items": [{"sku": "SKU-001", "qty": 1}], "coupon": "EXPIRED10"}


def test_n_plus_one_multiplies_queries_per_request(api):
    app_mod, client, state = api
    assert client.get("/orders").status_code == 200
    base = _last_request_log(state, "list_orders")["db_queries"]
    deployctl.deploy("api", "1.5.0", state=state)
    assert client.get("/orders").status_code == 200
    bad = _last_request_log(state, "list_orders")
    assert base == 2 and bad["db_queries"] == 31 and bad["version"] == "1.5.0"


def test_unknown_coupon_is_ignored_until_strict_release(api):
    app_mod, client, state = api
    assert client.post("/orders", json=ORDER).status_code == 200
    deployctl.deploy("api", "1.5.1", state=state)
    r = client.post("/orders", json=ORDER, headers={})
    assert r.status_code == 500
    rec = _last_request_log(state, "create_order")
    assert "KeyError" in rec["error"] and "apply_coupon" in rec["stack"]


def test_small_pool_times_out_instead_of_hanging(api):
    app_mod, client, state = api
    deployctl.set_config("api", "db_pool_size", 1, state=state)
    deployctl.set_config("api", "db_pool_timeout_s", 0.05, state=state)
    pool = app_mod.get_pool()
    with pool.acquire():
        r = client.get("/orders")
    assert r.status_code == 503
    assert "PoolTimeout" in _last_request_log(state, "list_orders")["error"]


def test_leak_grows_with_release_and_frees_on_rollback(api):
    app_mod, client, state = api
    deployctl.deploy("api", "1.5.2", state=state)
    for _ in range(3):
        client.post("/orders", json={**ORDER, "coupon": None})
    assert len(app_mod._PRICE_CACHE) == 3
    deployctl.deploy("api", "1.4.1", state=state, rollback=True)
    client.post("/orders", json={**ORDER, "coupon": None})
    assert len(app_mod._PRICE_CACHE) == 0


def test_payments_latency_is_visible_in_logs_not_in_changes(api):
    app_mod, client, state = api
    deployctl.set_fault("payments_latency_ms", 600, state=state)
    assert client.post("/orders", json={**ORDER, "coupon": None}).status_code == 200
    lines = [json.loads(l) for l in (state / "logs" / "api.jsonl").read_text().splitlines()]
    assert any(r["msg"] == "payments provider slow" for r in lines)
