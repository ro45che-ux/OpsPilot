"""Gemini adapter, with a fake HTTP transport (no network, no key): translation, schema, retry, quota."""
from __future__ import annotations

import json

import httpx
import pytest

from opspilot import evals
from opspilot.agents import llm
from opspilot.agents.llm import GeminiClient, LLMAgent, LLMError, gemini_schema
from opspilot.findings import submit_schema
from tests.helpers import StubEnv


def client(handler, **kw):
    return GeminiClient("k", transport=httpx.MockTransport(handler), min_interval_s=0, sleep=kw.pop("sleep", lambda s: None), **kw)


def ok(parts, usage=None):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
                                     "usageMetadata": usage or {"promptTokenCount": 11, "candidatesTokenCount": 5}})


TOOLS = [{"name": "find_anomalies", "description": "d", "input_schema": {"type": "object", "properties": {}}},
         {"name": "submit_findings", "description": "s", "input_schema": submit_schema()}]


def test_schema_is_converted_to_the_gemini_subset():
    g = gemini_schema(submit_schema())
    text = json.dumps(g)
    assert "additionalProperties" not in text and '"type": "string"' not in text and '"STRING"' in text
    action = g["properties"]["proposals"]["items"]["properties"]["action"]
    assert "anyOf" in action["properties"]["value"]  # the untyped "any JSON value" property got a concrete type


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(req):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers["x-goog-api-key"], json.loads(req.content)
        return ok([{"text": "checking"}, {"functionCall": {"name": "find_anomalies", "args": {}}, "thoughtSignature": "sig1"}])

    r = client(handler).create(model="gemini-x", system="sys", messages=[{"role": "user", "content": "go"}], tools=TOOLS,
                               max_tokens=100, force_tool="submit_findings")
    assert seen["url"].endswith("/models/gemini-x:generateContent") and seen["key"] == "k"
    b = seen["body"]
    assert b["systemInstruction"]["parts"][0]["text"] == "sys" and b["contents"] == [{"role": "user", "parts": [{"text": "go"}]}]
    assert b["toolConfig"]["functionCallingConfig"] == {"mode": "ANY", "allowedFunctionNames": ["submit_findings"]}
    assert [x.type for x in r.blocks] == ["text", "tool_use"] and r.blocks[1].name == "find_anomalies"
    assert (r.input_tokens, r.output_tokens, r.stop_reason) == (11, 5, "tool_use")


def test_tool_results_become_function_responses_and_thought_signatures_survive():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return ok([{"functionCall": {"name": "find_anomalies", "args": {}}, "thoughtSignature": "sig1"}]) if len(bodies) == 1 \
            else ok([{"text": "done"}])

    c = client(handler)
    first = c.create(model="m", system="s", messages=[{"role": "user", "content": "go"}], tools=TOOLS, max_tokens=10)
    use = first.blocks[0]
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": [use.to_api()]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": '{"anomalies": []}'},
                                            ]}]
    c.create(model="m", system="s", messages=history, tools=TOOLS, max_tokens=10)
    contents = bodies[1]["contents"]
    assert contents[1]["role"] == "model" and contents[1]["parts"][0]["thoughtSignature"] == "sig1"
    fr = contents[2]["parts"][0]["functionResponse"]
    assert contents[2]["role"] == "user" and fr["name"] == "find_anomalies" and "anomalies" in fr["response"]["result"]


def test_rate_limit_is_retried_using_the_servers_delay():
    calls, slept = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text='{"error":{"details":[{"retryDelay":"7s"}]}}')
        return ok([{"text": "hi"}])

    r = client(handler, sleep=slept.append).create(model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=5)
    assert r.blocks[0].text == "hi" and len(calls) == 3 and slept == [8.0, 8.0]


def test_daily_quota_and_auth_errors_fail_fast_without_retrying():
    n = []
    for status, text in [(429, "Quota exceeded ... GenerateRequestsPerDayPerProjectPerModel"), (403, "API key not valid")]:
        n.clear()
        with pytest.raises(LLMError):
            client(lambda req, status=status, text=text: (n.append(1), httpx.Response(status, text=text))[1]).create(
                model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=5)
        assert len(n) == 1


def test_calls_are_spaced_to_respect_the_free_tier():
    slept = []
    c = GeminiClient("k", transport=httpx.MockTransport(lambda r: ok([{"text": "a"}])), min_interval_s=6.5, sleep=slept.append)
    for _ in range(2):
        c.create(model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=5)
    assert slept and 0 < slept[0] <= 6.5


def test_provider_selection(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPSPILOT_PROVIDER", "OPSPILOT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert llm.provider() is None and not llm.ready()[0]
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm.provider() == "gemini" and llm.ready()[0] and llm.default_model().startswith("gemini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    assert llm.provider() == "anthropic"  # an explicit Claude key wins unless OPSPILOT_PROVIDER says otherwise
    monkeypatch.setenv("OPSPILOT_PROVIDER", "gemini")
    assert llm.provider() == "gemini"


def test_model_outage_is_reported_as_unavailable_not_as_a_wrong_answer():
    class Down:
        def create(self, **kw):
            raise LLMError("quota")

    res = LLMAgent(Down(), model="m").run(StubEnv())
    assert res.findings is None and res.error.startswith("llm_unavailable")


def test_outage_episodes_are_excluded_from_scores_and_counted():
    rows = [{"agent": "llm", "scenario": "a", "run": 0, "success": 1, "infra_error": False},
            {"agent": "llm", "scenario": "b", "run": 0, "success": 0, "infra_error": True}]
    m = evals.aggregate(rows)["llm"]
    assert m["episodes"] == 1 and m["infra_errors"] == 1 and m["success"] == 1.0


def test_network_timeouts_are_retried_then_reported_not_crashed():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow", request=req)
        return ok([{"text": "back"}])

    r = client(handler).create(model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=5)
    assert r.blocks[0].text == "back" and len(calls) == 3

    def always(req):
        raise httpx.ConnectError("down", request=req)
    with pytest.raises(LLMError):
        client(always).create(model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=5)


def test_eval_checkpoint_survives_a_crash_and_resume_reuses_finished_episodes(tmp_path):
    ck = tmp_path / "ck.jsonl"
    ids = ["bad_deploy_n_plus_one", "config_db_pool_too_small"]
    evals.run_eval(["heuristic"], ids[:1], checkpoint=ck, log=lambda m: None)   # "crashes" after one episode
    assert len(ck.read_text().splitlines()) == 1
    msgs = []
    rows = evals.run_eval(["heuristic"], ids, checkpoint=ck, resume=True, log=msgs.append)
    assert len(rows) == 2 and any("checkpoint" in m for m in msgs) and len(ck.read_text().splitlines()) == 2
    evals.run_eval(["heuristic"], ids[:1], checkpoint=ck, log=lambda m: None)   # no --resume: starts clean
    assert len(ck.read_text().splitlines()) == 1
