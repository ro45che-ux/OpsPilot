"""opspilot CLI: list / check scenarios, record snapshots, validate them."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from opspilot import scenarios
from opspilot.validate import validate_scenario

DEFAULT_OUT = scenarios.ROOT / "data" / "snapshots"


def _ids(arg: str) -> list[str]:
    all_ids = list(scenarios.load_all())
    if arg == "all":
        return all_ids
    if arg not in all_ids:
        sys.exit(f"unknown scenario '{arg}'. known: {', '.join(all_ids)}")
    return [arg]


def cmd_list(_a) -> int:
    for sc in scenarios.load_all().values():
        fix = "fix+nofix" if sc.has_fix_branch else "nofix only"
        print(f"{sc.id:42} {sc.difficulty:7} {sc.ground_truth.root_cause.category:22} {fix}")
    return 0


def cmd_check(_a) -> int:
    bad = 0
    for sc in scenarios.load_all().values():
        for p in scenarios.check_consistency(sc):
            print(f"{sc.id}: {p}")
            bad += 1
    print("scenario definitions OK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def cmd_record(a) -> int:
    from opspilot.recorder import record  # imported lazily: pulls in the process manager
    for sid in _ids(a.scenario):
        sc = scenarios.load(sid)
        branches = ["nofix", "fix"] if a.branch == "both" else [a.branch]
        for b in branches:
            if b == "fix" and not sc.has_fix_branch:
                continue
            record(sid, b, Path(a.out), speed=a.speed)
    return 0


def cmd_validate(a) -> int:
    failed = 0
    for sid in _ids(a.scenario):
        problems, observed = validate_scenario(sid, Path(a.out))
        status = "FAIL" if problems else "ok"
        print(f"{status:4} {sid}")
        if a.verbose:
            print("     " + json.dumps(observed, default=lambda x: round(x, 3)))
        for p in problems:
            print(f"     - {p}")
        failed += bool(problems)
    return 1 if failed else 0


def _approver(kind: str):
    from opspilot import incident
    return {"auto": incident.AutoApprover, "reject": incident.RejectApprover, "ask": incident.CLIApprover,
            "web": incident.StoreApprover}[kind]()


def _store(path: str):
    from opspilot.store import Store
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Store(path)


def cmd_investigate(a) -> int:
    from opspilot import evals, report
    from opspilot.agents import llm
    from opspilot.env import ReplayEnv
    from opspilot.incident import run_incident
    store = _store(a.db)
    if a.agent in evals.NEEDS_LLM and not llm.ready()[0]:
        sys.exit(f"agent '{a.agent}' needs an LLM: {llm.ready()[1]}")
    agent = evals.make_agent(a.agent, model=a.model)
    mode = evals.ENV_MODE.get(a.agent, "full")
    if a.live:
        from opspilot.live import run_live
        out = run_live(a.scenario, agent, store, _approver(a.approve), mode)
    else:
        env = ReplayEnv(a.scenario, Path(a.snapshots), mode)
        print(f"ALERT: {env.alert_summary()}")
        out = run_incident(env, agent, store, _approver(a.approve), a.scenario)
    print(f"\nincident {out.incident_id}: {out.status.upper()} - {out.reason}")
    if out.verification:
        print("verification:", out.verification["verdict"], out.verification["checks"])
    print("\n" + report.render(store, out.incident_id))
    return 0


def cmd_eval(a) -> int:
    from opspilot import evals
    from opspilot.agents import llm
    agents = a.agents.split(",")
    missing = [x for x in agents if x in evals.NEEDS_LLM] if not llm.ready()[0] else []
    if missing:
        print(f"skipping {missing}: {llm.ready()[1]}")
        agents = [x for x in agents if x not in missing]
    ids = None if a.scenarios == "all" else a.scenarios.split(",")
    rows = evals.run_eval(agents, ids, a.runs, Path(a.snapshots), model=a.model,
                          checkpoint=Path(a.results) / "checkpoint.jsonl", resume=a.resume)
    md = evals.save(rows, a.runs, a.model, Path(a.results))
    print("\n" + md.read_text())
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    from opspilot.web import create_app
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_report(a) -> int:
    from opspilot import report
    print(report.render(_store(a.db), a.incident))
    return 0


def cmd_resume(a) -> int:
    from opspilot import incident, report
    from opspilot.env import ReplayEnv
    store = _store(a.db)
    inc = store.get(a.incident)
    env = ReplayEnv(inc["scenario"], Path(a.snapshots))
    out = incident.resume(env, store, _approver(a.approve), a.incident)
    print(f"incident {out.incident_id}: {out.status.upper()} - {out.reason}")
    print(report.render(store, a.incident))
    return 0


def cmd_audit(a) -> int:
    ok, bad = _store(a.db).verify_chain()
    print("audit chain intact" if ok else f"audit chain BROKEN at seq {bad}")
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="opspilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenarios").set_defaults(fn=cmd_list)
    sub.add_parser("check", help="static consistency checks on scenario files").set_defaults(fn=cmd_check)
    r = sub.add_parser("record", help="run a scenario against the local testbed and save a snapshot")
    r.add_argument("scenario", help="scenario id or 'all'")
    r.add_argument("--branch", choices=["nofix", "fix", "both"], default="both")
    r.add_argument("--speed", type=float, default=1.0, help="scale all durations (0.5 = twice as fast)")
    r.add_argument("--out", default=str(DEFAULT_OUT))
    r.set_defaults(fn=cmd_record)
    v = sub.add_parser("validate", help="check snapshots show the symptoms the ground truth claims")
    v.add_argument("scenario", help="scenario id or 'all'")
    v.add_argument("--out", default=str(DEFAULT_OUT))
    v.add_argument("-v", "--verbose", action="store_true")
    v.set_defaults(fn=cmd_validate)
    db = str(scenarios.ROOT / "data" / "opspilot.db")
    snaps = str(DEFAULT_OUT)
    i = sub.add_parser("investigate", help="run an agent on a scenario: investigate, approve, execute, verify")
    i.add_argument("scenario")
    i.add_argument("--agent", default="heuristic", choices=["heuristic", "llm", "llm-basic", "single-prompt", "null", "oracle"])
    i.add_argument("--approve", default="ask", choices=["ask", "auto", "reject", "web"])
    i.add_argument("--live", action="store_true", help="use the real running testbed instead of a recorded snapshot")
    i.add_argument("--model", default=None)
    i.add_argument("--db", default=db)
    i.add_argument("--snapshots", default=snaps)
    i.set_defaults(fn=cmd_investigate)
    e = sub.add_parser("eval", help="grade agents over the scenario library")
    e.add_argument("--agents", default="null,heuristic,oracle")
    e.add_argument("--scenarios", default="all")
    e.add_argument("--runs", type=int, default=1)
    e.add_argument("--model", default=None)
    e.add_argument("--resume", action="store_true", help="reuse finished episodes from results/checkpoint.jsonl (same model only)")
    e.add_argument("--snapshots", default=snaps)
    e.add_argument("--results", default=str(scenarios.ROOT / "results"))
    e.set_defaults(fn=cmd_eval)
    sv = sub.add_parser("serve", help="web UI: incidents, evidence, approvals")
    sv.add_argument("--db", default=db)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)
    rp = sub.add_parser("report", help="markdown report for an incident")
    rp.add_argument("incident")
    rp.add_argument("--db", default=db)
    rp.set_defaults(fn=cmd_report)
    rs = sub.add_parser("resume", help="continue an incident that is awaiting approval")
    rs.add_argument("incident")
    rs.add_argument("--approve", default="web", choices=["ask", "auto", "reject", "web"])
    rs.add_argument("--db", default=db)
    rs.add_argument("--snapshots", default=snaps)
    rs.set_defaults(fn=cmd_resume)
    au = sub.add_parser("audit-verify", help="verify the audit log hash chain")
    au.add_argument("--db", default=db)
    au.set_defaults(fn=cmd_audit)
    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
