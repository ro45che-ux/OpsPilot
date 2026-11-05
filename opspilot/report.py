"""Human-readable incident report generated purely from the audit log: nothing here is
re-derived or reworded by a model, so the report cannot disagree with what actually happened."""
from __future__ import annotations

import time

from opspilot.store import Store


def _t(ts: float, t0: float) -> str:
    return f"+{ts - t0:5.1f}s"


def render(store: Store, iid: str) -> str:
    inc = store.get(iid)
    log = store.audit_log(iid)
    t0 = log[0]["ts"] if log else time.time()
    st = inc["state"]
    f = st.get("findings")
    out = [f"# Incident {iid}", "", f"- scenario: `{inc['scenario']}`", f"- final status: **{inc['status']}**",
           f"- audit chain intact: {'yes' if store.verify_chain()[0] else '**NO**'}", ""]
    if f:
        top = f["hypotheses"][0]
        out += ["## Root cause", "", f"**{top['category']}** in `{top['service']}` (confidence {top['confidence']:.0%})"
                + (f", change `{top['change_id']}`" if top.get("change_id") else ""), "", top["summary"], "",
                "Evidence cited: " + ", ".join(f"`{e}`" for e in top["evidence_ids"]), ""]
        for e in top["evidence_ids"]:
            ev = st.get("evidence", {}).get(e)
            if ev:
                out.append(f"- `{e}` = `{ev['tool']}({', '.join(f'{k}={v}' for k, v in ev['args'].items())})`")
        if len(f["hypotheses"]) > 1:
            out += ["", "Alternatives considered:"] + [
                f"- {h['category']} / {h['service']} ({h['confidence']:.0%}): {h['summary']}" for h in f["hypotheses"][1:]]
        out.append("")
    out += ["## Timeline", "", "| t | actor | event | detail |", "|---|---|---|---|"]
    for e in log:
        p = e["payload"]
        detail = {
            "policy_decision": lambda: f"{p.get('verdict')}: {p['action'].get('type')} - {'; '.join(p.get('reasons', []))}",
            "approval_requested": lambda: p.get("plan", ""),
            "executed": lambda: str(p.get("action")),
            "verification": lambda: f"{p.get('verdict')} " + ", ".join(
                f"{c['metric']} {c['before']}->{c['after']} (need {c['op']}{c['threshold']})" for c in p.get("checks", [])),
            "success_criteria_declared": lambda: ", ".join(f"{c['metric']} {c['op']} {c['value']}" for c in p.get("criteria", [])),
            "agent_finished": lambda: f"tool calls {p['stats'].get('tool_calls')}, tokens {p['stats'].get('input_tokens', 0)}/{p['stats'].get('output_tokens', 0)}"
                                      + (f", error: {p['error']}" if p.get("error") else ""),
        }.get(e["event"], lambda: p.get("reason", "") if isinstance(p, dict) else "")()
        out.append(f"| {_t(e['ts'], t0)} | {e['actor']} | {e['event']} | {str(detail)[:160].replace('|', '/')} |")
    return "\n".join(out) + "\n"
