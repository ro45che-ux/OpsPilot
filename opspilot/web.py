"""Web app: JSON API + the single-page console in opspilot/ui.

The console can launch investigations (replay or live), stream the agent's tool calls as they happen,
take approvals, show the evidence, charts and audit chain, and run the benchmark. A run lives in a
background thread with its OWN Store connection; threads and the CLI only share the SQLite file, so
an approval clicked here unblocks a runner in this process or in another one, and survives a restart.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from opspilot import evals, incident as inc_mod, report, scenarios as scn
from opspilot.agents import llm
from opspilot.env import ReplayEnv, action_key
from opspilot.findings import Findings
from opspilot.schemas import Action
from opspilot.store import TERMINAL, Store

DEFAULT_DB = Path(os.environ.get("OPSPILOT_DB", "data/opspilot.db"))
UI_DIR = Path(__file__).resolve().parent / "ui"
PACE_S = 0.9  # seconds between agent steps when a run is launched "at demo speed"

AGENTS = [
    {"id": "heuristic", "label": "Runbook heuristic", "kind": "baseline", "llm": False,
     "desc": "Undo the newest change near the first anomaly. No LLM; fooled by coincidental deploys."},
    {"id": "llm", "label": "OpsPilot LLM agent", "kind": "agent", "llm": True,
     "desc": "One model, typed read-only tools, deterministic log/anomaly/change analysis, evidence-validated findings."},
    {"id": "llm-basic", "label": "LLM, raw tools only", "kind": "ablation", "llm": True,
     "desc": "Same model without the analysis layer (raw logs and metrics only). Measures what the analysis adds."},
    {"id": "single-prompt", "label": "Single prompt", "kind": "baseline", "llm": True,
     "desc": "One shot, no tools: everything pasted into a prompt. Measures what tool use adds."},
    {"id": "null", "label": "Always escalate", "kind": "baseline", "llm": False, "desc": "Never diagnoses. The floor."},
    {"id": "oracle", "label": "Oracle (cheats)", "kind": "selftest", "llm": False,
     "desc": "Reads the ground truth. Exists to prove the grader gives a perfect agent 100%."},
    {"id": "oracle-wrong", "label": "Oracle, wrong fix", "kind": "selftest", "llm": False,
     "desc": "Right diagnosis, deliberately wrong action. Proves the verifier catches a fix that does not work."},
]


class Decision(BaseModel):
    approve: bool
    by: str = "web-user"
    reason: str = ""


class RunRequest(BaseModel):
    scenario: str
    agent: str = "heuristic"
    mode: str = "replay"        # replay | live
    approval: str = "web"       # web (a human clicks) | auto (rubber stamp)
    pace: bool = True           # slow the steps down so a human can watch them


class EvalRequest(BaseModel):
    agents: list[str] = ["null", "heuristic", "oracle", "oracle-wrong"]
    runs: int = 1


def _llm_ready() -> tuple[bool, str]:
    return llm.ready()


def _top(state: dict) -> dict | None:
    f = state.get("findings")
    return f["hypotheses"][0] if f and f.get("hypotheses") else None


def scorecard(store: Store, inc: dict, audit: list[dict]) -> dict | None:
    """Grade a finished replay incident against the scenario's hidden ground truth."""
    st = inc["state"]
    if inc["status"] not in TERMINAL or not st.get("findings"):
        return None
    try:
        sc = scn.load(inc["scenario"])
    except KeyError:
        return None
    gt = sc.ground_truth
    f = Findings.model_validate(st["findings"])
    top = f.hypotheses[0]
    rc = gt.root_cause
    rca_ok = top.category == rc.category and top.service == rc.service and (rc.change_id is None or top.change_id == rc.change_id)
    executed = [Action.model_validate(e["payload"]["action"]) for e in audit if e["event"] == "executed"]
    correct = {action_key(a) for a in gt.correct_actions}
    wrong = {action_key(a) for a in gt.wrong_actions}
    proposed = f.proposals[0].action if f.proposals else None
    needs_fix = sc.has_fix_branch
    verdict = next((e["payload"]["verdict"] for e in reversed(audit) if e["event"] == "verification"), None)
    wrong_ran = any(action_key(a) in wrong for a in executed)
    right_end = (inc["status"] == "resolved" and not wrong_ran) if needs_fix else (inc["status"] == "escalated" and not executed)
    return {
        "expected": {"category": rc.category, "service": rc.service, "change_id": rc.change_id, "summary": rc.summary,
                     "correct_actions": [a.model_dump(mode="json", exclude_none=True) for a in gt.correct_actions],
                     "red_herrings": gt.red_herring_changes},
        "rca_correct": rca_ok,
        "proposed_action_correct": bool(proposed and (action_key(proposed) in correct or (
            proposed.type == "escalate" and any(a.type == "escalate" for a in gt.correct_actions)))),
        "wrong_action_executed": wrong_ran,
        "blamed_red_herring": top.change_id in gt.red_herring_changes if top.change_id else False,
        "verdict": verdict, "success": bool(rca_ok and right_end),
    }


def create_app(db_path: Path | str = DEFAULT_DB) -> FastAPI:
    db_path = Path(db_path)
    store = Store(db_path)
    app = FastAPI(title="OpsPilot")
    runs: dict[str, dict] = {}
    active: set[str] = set()          # incident ids with a runner thread in this process
    live_lock = threading.Lock()      # the live testbed binds fixed ports: one live run at a time
    eval_job: dict = {"running": False, "done": 0, "total": 0, "log": [], "error": None, "finished_at": None}

    # ---- meta ----------------------------------------------------------------------------
    @app.get("/api/meta")
    def meta():
        ok, why = _llm_ready()
        return {
            "scenarios": [{"id": s.id, "title": s.title, "difficulty": s.difficulty, "description": s.description.strip(),
                           "alert": s.alert.model_dump(mode="json"), "has_fix": s.has_fix_branch,
                           "snapshot": (evals.SNAPSHOTS / s.id / "nofix" / "meta.json").exists()} for s in scn.load_all().values()],
            "agents": [{**a, "available": (ok or not a["llm"]), "why": "" if (ok or not a["llm"]) else why} for a in AGENTS],
            "llm_ready": ok, "model": llm.default_model(), "provider": llm.provider(),
        }

    # ---- incidents -----------------------------------------------------------------------
    def _row(i: dict) -> dict:
        top = _top(i["state"])
        sc = scn.load_all().get(i["scenario"] or "")
        pend = i["state"].get("pending_approval")
        plan = store.approval(pend)["plan"] if pend else None
        return {"id": i["id"], "scenario": i["scenario"], "title": sc.title if sc else i["scenario"], "status": i["status"],
                "created_at": i["created_at"], "updated_at": i["updated_at"],
                "duration_s": round(i["updated_at"] - i["created_at"], 1),
                "severity": sc.alert.severity if sc else None, "alert": sc.alert.name if sc else None,
                "service": sc.alert.service if sc else None, "difficulty": sc.difficulty if sc else None,
                "root_cause": None if not top else {"category": top["category"], "service": top["service"],
                                                    "confidence": top["confidence"], "summary": top["summary"]},
                "pending_approval": pend, "pending_plan": plan, "active": i["id"] in active,
                "terminal": i["status"] in TERMINAL}

    @app.get("/api/incidents")
    def incidents():
        return [_row(i) for i in store.list_incidents()]

    @app.get("/api/incidents/{iid}")
    def incident(iid: str):
        try:
            inc = store.get(iid)
        except KeyError:
            raise HTTPException(404, "no such incident")
        audit = store.audit_log(iid)
        ok, bad = store.verify_chain()
        sc = scn.load_all().get(inc["scenario"] or "")
        st = {k: v for k, v in inc["state"].items() if k != "charts"}
        return {**inc, "state": st, "row": _row(inc), "audit": audit, "approvals": store.approvals_for(iid),
                "chain_ok": ok, "chain_bad_seq": bad, "charts": inc["state"].get("charts"),
                "scorecard": scorecard(store, inc, audit),
                "scenario_info": None if not sc else {"title": sc.title, "description": sc.description.strip(),
                                                      "difficulty": sc.difficulty, "alert": sc.alert.model_dump(mode="json")},
                "resumable": inc["status"] in ("proposed", "awaiting_approval") and iid not in active and bool(inc["scenario"])}

    @app.post("/api/approvals/{aid}")
    def decide(aid: str, d: Decision):
        try:
            store.decide(aid, d.approve, d.by, d.reason)
        except KeyError:
            raise HTTPException(404, "no such approval")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/incidents/{iid}/report", response_class=PlainTextResponse)
    def md(iid: str):
        try:
            return report.render(store, iid)
        except KeyError:
            raise HTTPException(404, "no such incident")

    @app.get("/api/feed")
    def feed(limit: int = 40):
        rows = store.db.execute("SELECT * FROM audit WHERE event NOT IN ('tool_call','agent_note') ORDER BY seq DESC LIMIT ?",
                                (limit,)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "incident_id": r["incident_id"], "event": r["event"], "actor": r["actor"],
                 "payload": json.loads(r["payload"])} for r in rows]

    @app.get("/api/audit/verify")
    def audit_verify():
        ok, bad = store.verify_chain()
        n = store.db.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
        last = store.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return {"ok": ok, "first_bad_seq": bad, "entries": n, "head": last["hash"] if last else None}

    # ---- launching runs ------------------------------------------------------------------
    def _spawn(rec: dict, body):
        def work():
            st = Store(db_path)
            try:
                agent = evals.make_agent(rec["agent"])
                approver = inc_mod.StoreApprover() if rec["approval"] == "web" else inc_mod.AutoApprover()
                pace = PACE_S if rec["pace"] else 0.0

                def started(iid: str):
                    rec["incident_id"] = iid
                    active.add(iid)

                if rec["mode"] == "live":
                    from opspilot.live import run_live
                    with live_lock:
                        out = run_live(rec["scenario"], agent, st, approver, evals.ENV_MODE.get(rec["agent"], "full"),
                                       log=lambda m: rec["log"].append(m), on_start=started, pace=pace)
                else:
                    env = ReplayEnv(rec["scenario"], evals.SNAPSHOTS, evals.ENV_MODE.get(rec["agent"], "full"))
                    env.pace = pace
                    rec["log"].append(f"replaying recorded telemetry for {rec['scenario']}")
                    out = inc_mod.run_incident(env, agent, st, approver, rec["scenario"], on_start=started)
                rec["status"], rec["reason"] = out.status, out.reason
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"
                iid = rec.get("incident_id")
                if iid:
                    try:
                        st.set_status(iid, "failed", reason=rec["error"])
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                rec["done"] = True
                if rec.get("incident_id"):
                    active.discard(rec["incident_id"])
        threading.Thread(target=work, daemon=True, name=rec["id"]).start()

    @app.post("/api/runs")
    def start_run(r: RunRequest):
        if r.scenario not in scn.load_all():
            raise HTTPException(404, "unknown scenario")
        spec = next((a for a in AGENTS if a["id"] == r.agent), None)
        if spec is None:
            raise HTTPException(400, "unknown agent")
        if spec["llm"] and not _llm_ready()[0]:
            raise HTTPException(400, f"agent '{r.agent}' needs an LLM: {_llm_ready()[1]}")
        if r.mode not in ("replay", "live") or r.approval not in ("web", "auto"):
            raise HTTPException(400, "bad mode or approval")
        if r.mode == "replay" and not (evals.SNAPSHOTS / r.scenario / "nofix" / "meta.json").exists():
            raise HTTPException(400, "no recorded snapshot for this scenario; run `opspilot record` first")
        if r.mode == "live" and live_lock.locked():
            raise HTTPException(409, "a live run is already using the testbed")
        rec = {"id": "run-" + uuid.uuid4().hex[:6], **r.model_dump(), "incident_id": None, "done": False, "error": None,
               "log": [], "started": time.time(), "status": None, "reason": None}
        runs[rec["id"]] = rec
        _spawn(rec, r)
        return {"run_id": rec["id"]}

    @app.get("/api/runs/{rid}")
    def get_run(rid: str):
        if rid not in runs:
            raise HTTPException(404, "no such run")
        return runs[rid]

    @app.post("/api/incidents/{iid}/resume")
    def resume(iid: str):
        try:
            inc = store.get(iid)
        except KeyError:
            raise HTTPException(404, "no such incident")
        if inc["status"] not in ("proposed", "awaiting_approval") or not inc["scenario"]:
            raise HTTPException(409, f"incident is {inc['status']}; nothing to resume")
        if iid in active:
            raise HTTPException(409, "a runner is already attached to this incident")
        active.add(iid)

        def work():
            st = Store(db_path)
            try:
                env = ReplayEnv(inc["scenario"], evals.SNAPSHOTS)
                inc_mod.resume(env, st, inc_mod.StoreApprover(), iid)
            except Exception as e:  # noqa: BLE001
                try:
                    st.set_status(iid, "failed", reason=f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                active.discard(iid)
        threading.Thread(target=work, daemon=True, name=f"resume-{iid}").start()
        return {"ok": True}

    # ---- benchmark -----------------------------------------------------------------------
    def _latest_eval() -> tuple[list[dict], float] | None:
        files = sorted(evals.RESULTS.glob("eval-*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text()), files[-1].stat().st_mtime

    @app.get("/api/eval")
    def get_eval():
        latest = _latest_eval()
        out = {"job": eval_job, "rows": [], "aggregate": {}, "scenarios": [], "generated_at": None, "models": {}}
        if latest:
            rows, mtime = latest
            out.update(rows=[{k: r.get(k) for k in ("agent", "scenario", "success", "rca_top1", "status", "verdict", "wrong_executed",
                                                    "red_herring", "tool_calls", "reason", "run")} for r in rows],
                       aggregate=evals.aggregate(rows), generated_at=mtime,
                       models={r["agent"]: r["model"] for r in rows if r.get("model")},
                       scenarios=list(dict.fromkeys(r["scenario"] for r in rows)))
        return out

    @app.post("/api/eval")
    def run_eval(r: EvalRequest):
        if eval_job["running"]:
            raise HTTPException(409, "an evaluation is already running")
        ok = _llm_ready()[0]
        agents = [a for a in r.agents if a in {x["id"] for x in AGENTS} and (ok or a not in evals.NEEDS_LLM)]
        if not agents:
            raise HTTPException(400, "no runnable agents selected")
        ids = list(scn.load_all())
        det = {"null", "heuristic", "oracle", "oracle-wrong"}
        total = sum(len(ids) * (1 if a in det else max(1, r.runs)) for a in agents)
        eval_job.update(running=True, done=0, total=total, log=[], error=None, agents=agents)

        def work():
            try:
                def log(m):
                    eval_job["done"] += 1
                    eval_job["log"] = (eval_job["log"] + [m])[-12:]
                rows = evals.run_eval(agents, None, max(1, r.runs), log=log)
                evals.save(rows, max(1, r.runs), out_dir=evals.RESULTS)
                eval_job["finished_at"] = time.time()
            except Exception as e:  # noqa: BLE001
                eval_job["error"] = f"{type(e).__name__}: {e}"
            finally:
                eval_job["running"] = False
        threading.Thread(target=work, daemon=True, name="eval").start()
        return {"ok": True, "total": total}

    # ---- UI ------------------------------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    class NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-store"
            return resp

    app.mount("/static", NoCacheStatic(directory=UI_DIR), name="static")

    @app.exception_handler(KeyError)
    async def _keyerr(_req, exc):
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    return app
