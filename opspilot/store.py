"""Durable incident state, approvals and a hash-chained, append-only audit log (SQLite).

The incident row is the resumable state machine: if the process dies while a human is deciding,
`resume` picks the incident up from `awaiting_approval` with its findings intact.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

STATUSES = ["detected", "investigating", "proposed", "awaiting_approval", "executing", "verifying",
            "resolved", "escalated", "failed"]
TRANSITIONS = {
    "detected": {"investigating"},
    "investigating": {"proposed", "escalated", "failed"},
    "proposed": {"awaiting_approval", "escalated", "failed"},
    "awaiting_approval": {"executing", "escalated", "failed"},
    "executing": {"verifying", "failed", "escalated"},
    "verifying": {"resolved", "escalated", "failed", "awaiting_approval"},
    "resolved": set(), "escalated": set(), "failed": set(),
}
TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, scenario TEXT, status TEXT NOT NULL,
    created_at REAL, updated_at REAL, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, incident_id TEXT, event TEXT,
    actor TEXT, payload TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, incident_id TEXT, action TEXT, risk TEXT, plan TEXT,
    status TEXT NOT NULL DEFAULT 'pending', requested_at REAL, decided_at REAL, decided_by TEXT, reason TEXT);
"""


class IllegalTransition(Exception):
    pass


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()  # one connection may be shared by web request threads
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # incidents -------------------------------------------------------------------------------
    def create_incident(self, scenario: str | None, state: dict | None = None) -> str:
        iid = "inc-" + uuid.uuid4().hex[:8]
        now = time.time()
        self.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?)",
                        (iid, scenario, "detected", now, now, json.dumps(state or {})))
        self.audit(iid, "incident_created", "system", {"scenario": scenario})
        return iid

    def get(self, iid: str) -> dict:
        r = self.db.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        if r is None:
            raise KeyError(iid)
        return {**dict(r), "state": json.loads(r["state"])}

    def list_incidents(self) -> list[dict]:
        return [{**dict(r), "state": json.loads(r["state"])}
                for r in self.db.execute("SELECT * FROM incidents ORDER BY created_at DESC")]

    def set_status(self, iid: str, status: str, actor: str = "system", **payload) -> None:
        cur = self.get(iid)["status"]
        if status not in TRANSITIONS[cur]:
            raise IllegalTransition(f"{cur} -> {status}")
        self.db.execute("UPDATE incidents SET status=?, updated_at=? WHERE id=?", (status, time.time(), iid))
        self.audit(iid, f"status:{status}", actor, {"from": cur, **payload})

    def update_state(self, iid: str, **fields) -> None:
        state = self.get(iid)["state"]
        state.update(fields)
        self.db.execute("UPDATE incidents SET state=?, updated_at=? WHERE id=?", (json.dumps(state, default=str), time.time(), iid))

    # audit -----------------------------------------------------------------------------------
    def audit(self, iid: str, event: str, actor: str, payload: dict | None = None) -> None:
        payload_s = json.dumps(payload or {}, sort_keys=True, default=str)
        with self._lock:
            self._append(iid, event, actor, payload_s)

    def _append(self, iid: str, event: str, actor: str, payload_s: str) -> None:
        ts = round(time.time(), 3)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else "genesis"
            h = hashlib.sha256(f"{prev}|{ts}|{iid}|{event}|{actor}|{payload_s}".encode()).hexdigest()
            self.db.execute("INSERT INTO audit(ts,incident_id,event,actor,payload,prev_hash,hash) VALUES(?,?,?,?,?,?,?)",
                            (ts, iid, event, actor, payload_s, prev, h))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def audit_log(self, iid: str | None = None) -> list[dict]:
        q, args = ("SELECT * FROM audit WHERE incident_id=? ORDER BY seq", (iid,)) if iid else ("SELECT * FROM audit ORDER BY seq", ())
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.db.execute(q, args)]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute every hash; returns (ok, first_bad_seq)."""
        prev = "genesis"
        for r in self.db.execute("SELECT * FROM audit ORDER BY seq"):
            h = hashlib.sha256(f"{prev}|{r['ts']}|{r['incident_id']}|{r['event']}|{r['actor']}|{r['payload']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != h:
                return False, r["seq"]
            prev = r["hash"]
        return True, None

    # approvals -------------------------------------------------------------------------------
    def request_approval(self, iid: str, action: dict, risk: dict, plan: str) -> str:
        aid = "apr-" + uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO approvals(id,incident_id,action,risk,plan,requested_at) VALUES(?,?,?,?,?,?)",
                        (aid, iid, json.dumps(action), json.dumps(risk), plan, time.time()))
        self.audit(iid, "approval_requested", "system", {"approval_id": aid, "action": action, "plan": plan, "risk": risk})
        return aid

    def decide(self, aid: str, approve: bool, by: str, reason: str = "") -> None:
        r = self.db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if r is None:
            raise KeyError(aid)
        if r["status"] != "pending":
            raise ValueError(f"approval {aid} already {r['status']}")
        status = "approved" if approve else "rejected"
        self.db.execute("UPDATE approvals SET status=?, decided_at=?, decided_by=?, reason=? WHERE id=?",
                        (status, time.time(), by, reason, aid))
        self.audit(r["incident_id"], f"approval_{status}", by, {"approval_id": aid, "reason": reason})

    def approval(self, aid: str) -> dict:
        r = self.db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if r is None:
            raise KeyError(aid)
        return {**dict(r), "action": json.loads(r["action"]), "risk": json.loads(r["risk"])}

    def approvals_for(self, iid: str) -> list[dict]:
        return [{**dict(r), "action": json.loads(r["action"]), "risk": json.loads(r["risk"])}
                for r in self.db.execute("SELECT * FROM approvals WHERE incident_id=? ORDER BY requested_at", (iid,))]
