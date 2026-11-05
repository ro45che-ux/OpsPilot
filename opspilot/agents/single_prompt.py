"""Baseline: one prompt, no tools, no iteration. The model gets a dump of everything we can fit
(raw log tail, latest metric values, change list with diffs) and must answer once.

This is what 'just paste the logs into an LLM' looks like. It has no clustering, no anomaly onset, no
change correlation and no ability to ask a follow-up question, which is the point of comparing."""
from __future__ import annotations

import json
import time

from pydantic import ValidationError

from opspilot import telemetry as T
from opspilot.agents.base import AgentResult
from opspilot.agents.llm import DEFAULT_MODEL, LLMClient, LLMError, _clip, default_client
from opspilot.findings import Findings, submit_schema, validate_findings

SYSTEM = ("You are an on-call engineer. Given a dump of telemetry for an incident, diagnose it and call "
          "submit_findings once. Cite the evidence_ids of the sections you relied on. Rollback targets must come "
          "from the config section's known_versions; config values from config_history. Use 'escalate' if the "
          "cause is outside our control.")


class SinglePromptAgent:
    def __init__(self, client: LLMClient | None = None, model: str = DEFAULT_MODEL, name: str = "single-prompt",
                 log_lines: int = 120):
        self.client, self.model, self.name, self.log_lines = client, model, name, log_lines

    def run(self, env) -> AgentResult:
        t0 = time.time()
        sections = {}
        sections["changes"] = env.call("list_changes", {"limit": 30})
        diffs = [env.call("get_change_diff", {"change_id": c["id"]})
                 for c in sections["changes"]["result"]["changes"][:4]]
        sections["config_api"] = env.call("get_config", {"service": "api"})
        sections["config_worker"] = env.call("get_config", {"service": "worker"})
        sections["raw_logs"] = env.call("search_logs", {"min_level": "INFO", "limit": 40})
        sections["raw_logs_errors"] = env.call("search_logs", {"min_level": "WARNING", "limit": 40})
        # latest value of every metric series, no baseline and no trend: all a one-shot dump can offer
        latest = {k: (lambda p: p[-1][1] if p else None)(env.tel.series(k, 12)) for k in T.SERIES}
        sections["latest_metrics"] = {"evidence_id": "ev-metrics", "result": latest}
        from opspilot.env import Evidence
        env.evidence["ev-metrics"] = Evidence("ev-metrics", "metrics_dump", {}, latest)

        body = f"ALERT: {json.dumps(env.alert_summary())}\n\n" + "\n\n".join(
            f"## {k} (evidence_id={v['evidence_id']})\n{_clip(v['result'])}" for k, v in sections.items()) + "\n\n" + "\n\n".join(
            f"## diff (evidence_id={d['evidence_id']})\n{_clip(d['result'])}" for d in diffs)
        client = self.client or default_client()
        tools = [{"name": "submit_findings", "description": "Submit the diagnosis.", "input_schema": submit_schema()}]
        try:
            resp = client.create(model=self.model, system=SYSTEM, messages=[{"role": "user", "content": body}],
                                 tools=tools, max_tokens=4096, force_tool="submit_findings")
        except LLMError as e:
            return AgentResult(None, {"tool_calls": 0, "turns": 0, "wall_s": round(time.time() - t0, 2)}, error=f"llm_unavailable: {e}")
        stats = {"tool_calls": 0, "turns": 1, "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                 "wall_s": round(time.time() - t0, 2)}
        use = next((b for b in resp.blocks if b.type == "tool_use"), None)
        if use is None:
            return AgentResult(None, stats, error="model did not submit findings")
        try:
            f = Findings.model_validate(use.input)
        except ValidationError as e:
            return AgentResult(None, stats, error=f"schema: {e.errors()[:3]}")
        errs = validate_findings(f, env)
        return AgentResult(None if errs else f, stats, [{"role": "tool", "name": "submit_findings", "errors": errs}],
                           "invalid findings: " + "; ".join(errs) if errs else None)
