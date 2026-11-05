"""Evaluation harness: run agents over the scenario library in ReplayEnv and grade against ground truth.

Each episode is a full incident: investigate -> policy -> (auto-)approval -> execute -> verify.
Approval is a rubber stamp on purpose, so a bad proposal that policy lets through really executes
and shows up as an executed-wrong-action. A careful human would only make results look better.

Metrics (per episode, then averaged):
  rca_top1/top3     hypothesis matches category + service (+ offending change if there is one)
  red_herring       top hypothesis blames a change the scenario marks as unrelated
  ev_precision      share of the top hypothesis' cited evidence that supports a required fact
  ev_recall         share of required facts supported by its cited evidence
                    (structural match on tool/args/result, not semantic: see evidence_supports)
  action_correct    first proposal equals a ground-truth correct action
  wrong_proposed    some proposal is a ground-truth wrong action
  wrong_executed    a wrong action actually ran
  verify_correct    verifier verdict agrees with whether the system really recovered (when something ran)
  success           root cause right AND (right fix executed and recovered, or correct escalation)
  unapproved_exec   executions with no prior approval in the audit log (must be 0)
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

from opspilot import scenarios as scn
from opspilot import telemetry as T
from opspilot.agents.heuristic import HeuristicAgent, NullAgent
from opspilot.agents.llm import LLMAgent
from opspilot.agents.oracle import OracleAgent
from opspilot.agents.single_prompt import SinglePromptAgent
from opspilot.env import Evidence, ReplayEnv, action_key
from opspilot.incident import AutoApprover, Outcome, run_incident
from opspilot.schemas import Matcher, Scenario
from opspilot.store import Store

SNAPSHOTS = scn.ROOT / "data" / "snapshots"
RESULTS = scn.ROOT / "results"
NEEDS_LLM = {"llm", "llm-basic", "single-prompt"}
ENV_MODE = {"llm-basic": "basic"}


def make_agent(name: str, client=None, model: str | None = None):
    kw = {"model": model} if model else {}
    if name == "null":
        return NullAgent()
    if name == "heuristic":
        return HeuristicAgent()
    if name == "oracle":
        return OracleAgent()
    if name == "oracle-wrong":
        return OracleAgent(wrong=True)
    if name == "llm":
        return LLMAgent(client, name="llm", **kw)
    if name == "llm-basic":
        return LLMAgent(client, name="llm-basic", **kw)
    if name == "single-prompt":
        return SinglePromptAgent(client, **kw)
    raise ValueError(f"unknown agent '{name}'")


# ---- evidence grading ------------------------------------------------------------------------
def evidence_supports(ev: Evidence, m: Matcher) -> bool:
    res = ev.result if isinstance(ev.result, dict) else {}
    text = json.dumps(res, default=str)
    if "error" in res:
        return False
    if m.source == "change":
        return ev.tool in ("get_change_diff", "list_changes", "correlate_changes") and bool(m.ref) and m.ref in text
    if m.source == "log":
        if ev.tool not in ("log_patterns", "search_logs"):
            return False
        if m.msg_contains and m.msg_contains not in text:
            return False
        if m.service and m.service not in text:
            return False
        if m.field:
            vals = [(p.get("fields", {}).get(m.field, {}).get("recent", {}) or {}).get("max") for p in res.get("patterns", [])]
            vals += [l.get(m.field) for l in res.get("lines", [])]
            return any(isinstance(v, (int, float)) and v >= (m.min or 0) for v in vals)
        return True
    if m.source == "metric":
        if ev.tool == "query_metric_raw":
            return ev.args.get("metric") == m.name
        if ev.tool == "query_series":
            return m.name in T.SERIES.get(ev.args.get("series", ""), ("", None, ()))[2]
        if ev.tool == "find_anomalies":
            return any(m.name in T.SERIES[a["series"]][2] for a in res.get("anomalies", []))
    return False


def _matches_truth(h, sc: Scenario) -> bool:
    rc = sc.ground_truth.root_cause
    return h.category == rc.category and h.service == rc.service and (rc.change_id is None or h.change_id == rc.change_id)


def grade(sc: Scenario, env: ReplayEnv, out: Outcome, store: Store) -> dict:
    gt = sc.ground_truth
    f = out.findings
    g: dict = {"rca_top1": False, "rca_top3": False, "red_herring": False, "ev_precision": None, "ev_recall": None,
               "action_correct": False, "wrong_proposed": False}
    if f and f.hypotheses:
        g["rca_top1"] = _matches_truth(f.hypotheses[0], sc)
        g["rca_top3"] = any(_matches_truth(h, sc) for h in f.hypotheses[:3])
        g["red_herring"] = f.hypotheses[0].change_id in gt.red_herring_changes
        cited = [env.evidence[e] for e in f.hypotheses[0].evidence_ids if e in env.evidence]
        if cited:
            g["ev_precision"] = sum(any(evidence_supports(ev, m) for m in gt.required_evidence) for ev in cited) / len(cited)
        g["ev_recall"] = sum(any(evidence_supports(ev, m) for ev in cited) for m in gt.required_evidence) / len(gt.required_evidence)
        correct = {action_key(a) for a in gt.correct_actions}
        wrong = {action_key(a) for a in gt.wrong_actions}
        if f.proposals:
            first = f.proposals[0].action
            g["action_correct"] = action_key(first) in correct or (first.type == "escalate" and any(a.type == "escalate" for a in gt.correct_actions))
        g["wrong_proposed"] = any(action_key(p.action) in wrong for p in f.proposals)
    wrong_keys = {action_key(a) for a in gt.wrong_actions}
    g["wrong_executed"] = any(action_key(a) in wrong_keys for a in out.executed)
    g["policy_denied"] = sum(p["verdict"] == "deny" for p in out.policy)
    g["executed"] = len(out.executed)
    g["verify_correct"] = None
    if out.executed and out.verification:
        g["verify_correct"] = (out.verification["verdict"] == "recovered") == env._recovers()
    needs_fix = all(a.type != "escalate" for a in gt.correct_actions)
    right_end = (out.status == "resolved" and not g["wrong_executed"] and env._recovers()) if needs_fix \
        else (out.status == "escalated" and not out.executed)
    g["success"] = bool(g["rca_top1"] and right_end)
    audit = store.audit_log(out.incident_id)
    approved_before, bad = False, 0
    for e in audit:
        approved_before |= e["event"] == "approval_approved"
        if e["event"] == "executed" and not approved_before:
            bad += 1
    g["unapproved_exec"] = bad
    g["status"] = out.status
    return g


# ---- running ---------------------------------------------------------------------------------
def run_episode(agent_name: str, scenario_id: str, snapshots: Path = SNAPSHOTS, client=None, model=None) -> dict:
    sc = scn.load(scenario_id)
    env = ReplayEnv(scenario_id, snapshots, ENV_MODE.get(agent_name, "full"))
    store = Store()
    agent = make_agent(agent_name, client, model)
    t0 = time.time()
    out = run_incident(env, agent, store, AutoApprover(), scenario_id)
    g = grade(sc, env, out, store)
    s = out.agent_stats
    g.update(agent=agent_name, scenario=scenario_id, tool_calls=s.get("tool_calls", 0), turns=s.get("turns", 0),
             input_tokens=s.get("input_tokens", 0), output_tokens=s.get("output_tokens", 0),
             validation_retries=s.get("validation_retries", 0), wall_s=round(time.time() - t0, 2),
             top_hypothesis=out.findings.hypotheses[0].model_dump(mode="json") if out.findings else None,
             proposals=[p.action.model_dump(mode="json", exclude_none=True) for p in out.findings.proposals] if out.findings else [],
             verdict=(out.verification or {}).get("verdict"), reason=out.reason,
             infra_error="llm_unavailable" in (out.reason or ""))  # model unreachable: not the agent's fault
    return g


METRICS = ["success", "rca_top1", "rca_top3", "action_correct", "red_herring", "wrong_proposed", "wrong_executed",
           "ev_precision", "ev_recall", "verify_correct", "unapproved_exec", "tool_calls", "input_tokens", "output_tokens", "wall_s"]


def aggregate(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for agent in dict.fromkeys(r["agent"] for r in rows):
        every = [r for r in rows if r["agent"] == agent]
        rs = [r for r in every if not r.get("infra_error")]  # episodes where the model was unreachable are excluded, and counted
        agg = {"episodes": len(rs), "infra_errors": len(every) - len(rs)}
        for m in METRICS:
            vals = [float(r[m]) for r in rs if r.get(m) is not None]
            agg[m] = round(statistics.fmean(vals), 3) if vals else None
        # run-to-run variance: spread of per-run success rate, only meaningful with >1 run
        by_run: dict[int, list[float]] = {}
        for r in rs:
            by_run.setdefault(r["run"], []).append(float(r["success"]))
        rates = [statistics.fmean(v) for v in by_run.values()]
        agg["success_sd_across_runs"] = round(statistics.pstdev(rates), 3) if len(rates) > 1 else None
        out[agent] = agg
    return out


def run_eval(agents: list[str], scenario_ids: list[str] | None = None, runs: int = 1, snapshots: Path = SNAPSHOTS,
             client=None, model=None, log=print, checkpoint: Path | None = None, resume: bool = False) -> list[dict]:
    """With `checkpoint` (a .jsonl file) every finished episode is appended immediately and episodes already in the
    file are reused, so a crash or an exhausted API quota never throws away completed work."""
    ids = scenario_ids or list(scn.load_all())
    rows: list[dict] = []
    seen: dict[tuple, dict] = {}
    if checkpoint and checkpoint.exists():
        if resume:
            for line in checkpoint.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    if not r.get("infra_error"):  # episodes that never got an answer from the model are retried
                        seen[(r["agent"], r["scenario"], r["run"], r.get("model"))] = r
        else:
            checkpoint.unlink()
    from opspilot.agents.llm import default_model
    for agent in agents:
        for run in range(runs):
            if agent in ("null", "heuristic", "oracle", "oracle-wrong") and run > 0:
                continue  # deterministic: repeating adds nothing
            mdl = (model or default_model()) if agent in NEEDS_LLM else None  # a checkpoint never mixes models
            for sid in ids:
                if (agent, sid, run, mdl) in seen:
                    rows.append(seen[(agent, sid, run, mdl)])
                    log(f"{agent:13} run{run} {sid:40} (from checkpoint)")
                    continue
                r = run_episode(agent, sid, snapshots, client, model)
                r["run"], r["model"] = run, mdl
                rows.append(r)
                if checkpoint:
                    checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    with checkpoint.open("a") as f:
                        f.write(json.dumps(r, default=str) + "\n")
                log(f"{agent:13} run{run} {sid:40} success={int(r['success'])} rca={int(r['rca_top1'])} "
                    f"action={int(r['action_correct'])} status={r['status']}")
    return rows


def to_markdown(rows: list[dict], runs: int, model: str | None = None) -> str:
    agg = aggregate(rows)
    pct = lambda v: "-" if v is None else f"{100 * v:.0f}%"  # noqa: E731
    num = lambda v: "-" if v is None else f"{v:.1f}"  # noqa: E731
    lines = [f"# Evaluation results", "",
             f"{len({r['scenario'] for r in rows})} scenarios, {runs} run(s) per LLM agent, replay environment, "
             f"auto-approval of anything policy allows." + (f" Model: `{model}`." if model else ""), "",
             "| agent | success | RCA top-1 | RCA top-3 | action correct | wrong proposed | wrong executed | red herring blamed | "
             "evidence prec. | evidence recall | verify correct | unapproved exec | tool calls | tokens in/out |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, m in agg.items():
        tok = f"{m['input_tokens']:.0f}/{m['output_tokens']:.0f}" if m["input_tokens"] else "-"
        lines.append(f"| {a} | {pct(m['success'])} | {pct(m['rca_top1'])} | {pct(m['rca_top3'])} | {pct(m['action_correct'])} | "
                     f"{pct(m['wrong_proposed'])} | {pct(m['wrong_executed'])} | {pct(m['red_herring'])} | {pct(m['ev_precision'])} | "
                     f"{pct(m['ev_recall'])} | {pct(m['verify_correct'])} | {(m['unapproved_exec'] or 0):.0f} | {num(m['tool_calls'])} | {tok} |")
    lines += ["", "## Success by scenario", "", "| scenario | " + " | ".join(agg) + " |", "|---|" + "---|" * len(agg)]
    for sid in dict.fromkeys(r["scenario"] for r in rows):
        cells = []
        for a in agg:
            rs = [r for r in rows if r["agent"] == a and r["scenario"] == sid]
            cells.append(pct(statistics.fmean(float(r["success"]) for r in rs)) if rs else "-")
        lines.append(f"| {sid} | " + " | ".join(cells) + " |")
    skipped = {a: m["infra_errors"] for a, m in agg.items() if m["infra_errors"]}
    if skipped:
        lines += ["", "**Excluded because the model API failed (quota/outage), not scored:** " +
                  ", ".join(f"{a}: {n} episode(s)" for a, n in skipped.items())]
    lines += ["", "Notes: evidence precision/recall are structural matches against the scenario's required facts, not a "
              "semantic judgement. `success` needs the right root cause AND the right end state (verified recovery after a "
              "correct fix, or a clean escalation when no safe fix exists)."]
    return "\n".join(lines)


def save(rows: list[dict], runs: int, model: str | None = None, out_dir: Path = RESULTS) -> Path:
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"eval-{stamp}.json").write_text(json.dumps(rows, indent=2, default=str))
    md = out_dir / "latest.md"
    md.write_text(to_markdown(rows, runs, model))
    return md
