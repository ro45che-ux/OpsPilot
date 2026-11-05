"""LLM investigation agent: one model, one loop, typed tools, hard budgets, validated output.

No planner/critic/executor swarm: the model calls read-only telemetry tools, then must call
`submit_findings`. The harness rejects findings that cite nonexistent evidence (the model gets the
errors back and may retry), caps tool calls, and forces a final submission when the budget is gone.
It can only *propose* actions; the policy gate and a human decide whether anything runs.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from opspilot.agents.base import AgentResult
from opspilot.findings import Findings, submit_schema, validate_findings



class LLMError(Exception):
    """The model could not be reached or refused the request (quota, auth, outage). Not an agent mistake."""


def provider() -> str | None:
    forced = os.environ.get("OPSPILOT_PROVIDER")
    if forced in ("anthropic", "gemini"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return None


def ready() -> tuple[bool, str]:
    p = provider()
    if p is None:
        return False, "set ANTHROPIC_API_KEY, or GEMINI_API_KEY (free tier)"
    if p == "anthropic":
        import importlib.util
        if importlib.util.find_spec("anthropic") is None:
            return False, "pip install -e '.[llm]'"
    return True, ""


def default_model() -> str:
    return os.environ.get("OPSPILOT_MODEL") or ("gemini-2.5-flash" if provider() == "gemini" else "claude-sonnet-5-5")


DEFAULT_MODEL = default_model()
MAX_RESULT_CHARS = 7000

SYSTEM = """You are OpsPilot, an on-call engineer investigating a production incident in a small \
orders system (services: api, worker; third party: payments). You can only read telemetry; you cannot \
change anything. A separate policy gate and a human decide what runs.

Method:
1. Start with find_anomalies and correlate_changes, then log_patterns, to see what moved and when.
2. State at least two competing hypotheses before you settle. Pick the next query that would best tell \
them apart (a diff, a series, a log pattern), not the one that confirms your favourite.
3. A change that merely precedes the symptom is a lead, not proof. Read its diff and check it touches the \
failing path. Also consider causes that are NOT in the change list: third-party dependencies, bad data, \
load. list_changes does not show those.
4. Do not stop at a symptom. Pool exhaustion, high latency or errors are often downstream of something else.
5. Cite evidence: every hypothesis needs evidence_ids returned by your tool calls. Invented IDs are rejected.
6. Propose the least invasive reversible action that addresses the cause: revert a flag/config before rolling \
back a deploy, roll back before restarting. A rollback target must be in get_config.known_versions and a \
config value must come from get_config.config_history. If the cause is outside our control, or no safe \
fix exists, propose `escalate`. Restarting only hides a leak or a bad deploy.
7. Set success_criteria with thresholds that separate the healthy baseline from the degraded level \
(find_anomalies/query_series show both). Recovery will be judged against them.

Be efficient: you have a limited number of tool calls. Finish by calling submit_findings."""


@dataclass
class Block:
    type: str                  # "text" | "tool_use"
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def to_api(self) -> dict:
        return ({"type": "text", "text": self.text} if self.type == "text"
                else {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input})


@dataclass
class LLMResponse:
    blocks: list[Block]
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def create(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int,
               force_tool: str | None = None) -> LLMResponse: ...


class AnthropicClient:
    def __init__(self):
        import anthropic  # optional dependency: pip install -e '.[llm]'
        self._c = anthropic.Anthropic()

    def create(self, *, model, system, messages, tools, max_tokens, force_tool=None) -> LLMResponse:
        kw = {"tool_choice": {"type": "tool", "name": force_tool}} if force_tool else {}
        r = self._c.messages.create(model=model, system=system, messages=messages, tools=tools,
                                    max_tokens=max_tokens, **kw)
        blocks = [Block("text", text=b.text) if b.type == "text" else Block("tool_use", id=b.id, name=b.name, input=b.input)
                  for b in r.content if b.type in ("text", "tool_use")]
        return LLMResponse(blocks, r.stop_reason, r.usage.input_tokens, r.usage.output_tokens)


_GEM_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def gemini_schema(s):
    """JSON Schema -> the OpenAPI subset Gemini accepts: upper-case types, no additionalProperties/title/default,
    and an untyped property (any JSON value) becomes anyOf string/number/boolean."""
    if isinstance(s, list):
        return [gemini_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k in ("additionalProperties", "$schema", "title", "default", "examples"):
            continue
        if k == "type" and isinstance(v, str) and v in _GEM_TYPES:
            out[k] = v.upper()
        elif k == "properties":
            out[k] = {n: gemini_schema(p) for n, p in v.items()}
        else:
            out[k] = gemini_schema(v)
    if "type" not in out and "anyOf" not in out and "properties" not in out:
        out["anyOf"] = [{"type": "STRING"}, {"type": "NUMBER"}, {"type": "BOOLEAN"}]
    return out


class GeminiClient:
    """Google Gemini over plain REST (httpx), speaking the same Block/LLMResponse interface as AnthropicClient.

    Built for the free tier: calls are spaced (OPSPILOT_MIN_INTERVAL_S, default 6.5 s ~ 9 requests/minute),
    429/5xx are retried honouring the server's retryDelay, and a daily-quota error fails fast with a clear message."""
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str | None = None, transport=None, min_interval_s: float | None = None, sleep=None):
        import threading
        import httpx
        self.key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.key:
            raise LLMError("no GEMINI_API_KEY set")
        self.http = httpx.Client(transport=transport, timeout=120)
        self.min_interval = float(os.environ.get("OPSPILOT_MIN_INTERVAL_S", "6.5")) if min_interval_s is None else min_interval_s
        self._sleep = sleep or time.sleep
        self._last = 0.0
        self._lock = threading.Lock()
        self._raw: dict[str, dict] = {}   # tool_use id -> the exact functionCall part (keeps thoughtSignature)
        self._n = 0

    def _contents(self, messages: list[dict]) -> list[dict]:
        names: dict[str, str] = {}
        out = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": role, "parts": [{"text": c}]})
                continue
            parts = []
            for b in c:
                if b["type"] == "text":
                    if b["text"].strip():
                        parts.append({"text": b["text"]})
                elif b["type"] == "tool_use":
                    names[b["id"]] = b["name"]
                    parts.append(self._raw.get(b["id"]) or {"functionCall": {"name": b["name"], "args": b["input"]}})
                elif b["type"] == "tool_result":
                    body = {"error": b["content"]} if b.get("is_error") else {"result": b["content"]}
                    parts.append({"functionResponse": {"name": names.get(b["tool_use_id"], "unknown"), "response": body}})
            if parts:
                out.append({"role": role, "parts": parts})
        return out

    def _post(self, model: str, body: dict) -> dict:
        import re
        import httpx
        url = f"{self.BASE}/models/{model}:generateContent"
        for attempt in range(7):
            with self._lock:
                wait = self.min_interval - (time.time() - self._last)
                if wait > 0:
                    self._sleep(wait)
                self._last = time.time()
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self.key})
            except (httpx.TransportError, httpx.TimeoutException) as e:  # dropped connection, read timeout, DNS blip
                if attempt < 6:
                    self._sleep(min(60, 5 * 2 ** attempt))
                    continue
                raise LLMError(f"Gemini network error after retries: {type(e).__name__}: {e}")
            if r.status_code == 200:
                return r.json()
            msg = r.text[:600]
            if r.status_code == 429 and re.search(r"PerDay|per day|daily", msg, re.I):
                raise LLMError(f"Gemini daily quota exhausted: {msg}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 6:
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', msg)
                self._sleep(float(m.group(1)) + 1 if m else min(60, 5 * 2 ** attempt))
                continue
            raise LLMError(f"Gemini HTTP {r.status_code}: {msg}")
        raise LLMError("Gemini: retries exhausted")

    def create(self, *, model, system, messages, tools, max_tokens, force_tool=None) -> LLMResponse:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(messages),
            "tools": [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"], "parameters": gemini_schema(t["input_schema"])} for t in tools]}],
            "generationConfig": {"maxOutputTokens": max(max_tokens, 8192)},
        }
        if force_tool:
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [force_tool]}}
        data = self._post(model, body)
        if data.get("promptFeedback", {}).get("blockReason"):
            raise LLMError(f"Gemini blocked the prompt: {data['promptFeedback']['blockReason']}")
        cand = (data.get("candidates") or [{}])[0]
        blocks: list[Block] = []
        for part in (cand.get("content") or {}).get("parts", []):
            if part.get("thought"):
                continue
            if "functionCall" in part:
                self._n += 1
                bid = f"gem-{self._n}"
                self._raw[bid] = part
                fc = part["functionCall"]
                blocks.append(Block("tool_use", id=bid, name=fc["name"], input=fc.get("args") or {}))
            elif part.get("text"):
                blocks.append(Block("text", text=part["text"]))
        u = data.get("usageMetadata", {})
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else (cand.get("finishReason") or "end_turn")
        return LLMResponse(blocks, stop, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0))


def default_client() -> LLMClient:
    return GeminiClient() if provider() == "gemini" else AnthropicClient()


def _clip(obj) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= MAX_RESULT_CHARS else s[:MAX_RESULT_CHARS] + '..."[truncated; narrow the query]"'


def brief(env) -> str:
    a = env.alert_summary()
    return (f"ALERT {a['name']} ({a['severity']}) on {a['service']}: {a['summary']}. Fired {a['fired_ago_s']}s ago.\n"
            "Investigate, then call submit_findings.")


class LLMAgent:
    def __init__(self, client: LLMClient | None = None, model: str = DEFAULT_MODEL, name: str = "llm",
                 max_tool_calls: int = 14, max_validation_retries: int = 2, max_tokens: int = 4096):
        self.client, self.model, self.name = client, model, name
        self.max_tool_calls, self.max_retries, self.max_tokens = max_tool_calls, max_validation_retries, max_tokens

    def run(self, env) -> AgentResult:
        client = self.client or default_client()
        t0 = time.time()
        tools = [{"name": s.name, "description": s.description, "input_schema": s.schema} for s in env.tools()]
        tools.append({"name": "submit_findings", "description": "Submit your final diagnosis and proposed action. "
                      "Ends the investigation.", "input_schema": submit_schema()})
        messages: list[dict] = [{"role": "user", "content": brief(env)}]
        transcript: list[dict] = []
        stats = {"tool_calls": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "validation_retries": 0}
        findings, last_errors = None, []

        def finish(error: str | None = None) -> AgentResult:
            stats["wall_s"] = round(time.time() - t0, 2)
            return AgentResult(findings, stats, transcript, error)

        force = False
        for _ in range(self.max_tool_calls + self.max_retries + 4):
            try:
                resp = client.create(model=self.model, system=SYSTEM, messages=messages, tools=tools,
                                     max_tokens=self.max_tokens, force_tool="submit_findings" if force else None)
            except LLMError as e:
                return finish(f"llm_unavailable: {e}")
            stats["turns"] += 1
            stats["input_tokens"] += resp.input_tokens
            stats["output_tokens"] += resp.output_tokens
            messages.append({"role": "assistant", "content": [b.to_api() for b in resp.blocks]})
            for b in resp.blocks:
                if b.type == "text" and b.text.strip():
                    transcript.append({"role": "assistant", "text": b.text})
                    if getattr(env, "on_note", None):
                        env.on_note(b.text.strip())
            uses = [b for b in resp.blocks if b.type == "tool_use"]
            if not uses:
                messages.append({"role": "user", "content": "Call a tool, or submit_findings if you are done."})
                force = stats["tool_calls"] >= self.max_tool_calls
                continue
            results, accepted = [], False
            for u in uses:
                if u.name == "submit_findings":
                    try:
                        cand = Findings.model_validate(u.input)
                        errs = validate_findings(cand, env)
                    except ValidationError as e:
                        cand, errs = None, [f"schema: {x['loc']}: {x['msg']}" for x in e.errors()[:6]]
                    transcript.append({"role": "tool", "name": u.name, "errors": errs})
                    if not errs:
                        findings, accepted = cand, True
                        results.append({"type": "tool_result", "tool_use_id": u.id, "content": "accepted"})
                    elif stats["validation_retries"] < self.max_retries:
                        stats["validation_retries"] += 1
                        last_errors = errs
                        results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                        "content": "rejected, fix and resubmit:\n- " + "\n- ".join(errs)})
                    else:
                        results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True, "content": "rejected"})
                        return finish("findings failed validation: " + "; ".join(errs))
                elif stats["tool_calls"] >= self.max_tool_calls:
                    results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                    "content": "tool budget exhausted; call submit_findings now"})
                    force = True
                else:
                    stats["tool_calls"] += 1
                    out = env.call(u.name, u.input)
                    transcript.append({"role": "tool", "name": u.name, "args": u.input, "evidence_id": out["evidence_id"]})
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": _clip(out)})
            messages.append({"role": "user", "content": results})
            if accepted:
                return finish()
            if stats["tool_calls"] >= self.max_tool_calls:
                force = True
        return finish("no valid findings within turn limit" + (f": {last_errors}" if last_errors else ""))
