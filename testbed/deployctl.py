"""Deploy / config / fault control for the testbed.

Every deploy, config edit and flag flip appends a change record to state/changes.jsonl: that is
the 'deployment history' the agent investigates. Environmental faults (payments latency, poison
jobs) are deliberately NOT recorded as changes, as in real life nobody logs those.

CLI:  python -m testbed.deployctl {init,deploy,config,fault,poison}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from testbed.common import db
from testbed.common.state import append_jsonl, read_json, read_jsonl, state_dir, write_json
from testbed.releases.catalog import BASELINE, HISTORY, RELEASES


def init_state(state: Path | None = None, now: float | None = None) -> Path:
    """Idempotent: create baseline release, history, faults file and seeded database."""
    state = Path(state) if state else state_dir()
    (state / "logs").mkdir(parents=True, exist_ok=True)
    now = now or time.time()

    if not (state / "release.json").exists():
        rel = {s: {**v, "behaviors": []} for s, v in BASELINE.items()}
        write_json(state / "release.json", rel)

    if not (state / "changes.jsonl").exists():
        for n, (age, kind, args) in enumerate(HISTORY, start=1):
            ts = now - age
            cid = f"chg-h{n:03d}"
            if kind == "deploy":
                service, version = args
                r = RELEASES[(service, version)]
                _record(state, cid, ts, service, "deploy", r.summary, r.author, r.diff,
                        to_version=version)
            else:
                service, key, old, new, summary, author = args
                _record(state, cid, ts, service, "config", summary, author, _config_diff(service, key, old, new),
                        key=key, old=old, new=new)

    if not (state / "faults.json").exists():
        write_json(state / "faults.json", {"payments_latency_ms": 0})

    db.init_db(state / "app.db")
    return state


def _record(state: Path, cid: str, ts: float, service: str, ctype: str, summary: str, author: str,
            diff: str, **details) -> dict:
    rec = {"id": cid, "ts": round(ts, 3), "service": service, "type": ctype,
           "summary": summary, "author": author, "diff": diff, **details}
    append_jsonl(state / "changes.jsonl", rec)
    return rec


def _config_diff(service: str, key: str, old, new) -> str:
    return f"--- a/config/{service}.yaml\n+++ b/config/{service}.yaml\n@@ @@\n-{key}: {json.dumps(old)}\n+{key}: {json.dumps(new)}\n"


def _next_id(state: Path) -> str:
    return f"chg-{len(read_jsonl(state / 'changes.jsonl')) + 1:04d}"


def deploy(service: str, version: str, *, state: Path | None = None, change_id: str | None = None,
           author: str | None = None, rollback: bool = False) -> dict:
    state = Path(state) if state else state_dir()
    rel = RELEASES[(service, version)]
    manifest = read_json(state / "release.json")
    cur = manifest[service]
    old = cur["version"]
    cur["version"] = version
    cur["behaviors"] = list(rel.behaviors)
    write_json(state / "release.json", manifest)
    summary = f"Rollback {service} {old} -> {version}" if rollback else rel.summary
    return _record(state, change_id or _next_id(state), time.time(), service, "deploy", summary,
                   author or ("ops.oncall" if rollback else rel.author), "" if rollback else rel.diff,
                   from_version=old, to_version=version, rollback=rollback)


def set_config(service: str, key: str, value, *, state: Path | None = None, change_id: str | None = None,
               summary: str | None = None, author: str = "ops.sam") -> dict:
    state = Path(state) if state else state_dir()
    manifest = read_json(state / "release.json")
    config = manifest[service]["config"]
    old = config.get(key)
    config[key] = value
    write_json(state / "release.json", manifest)
    ctype = "flag" if key.startswith("flags.") else "config"
    return _record(state, change_id or _next_id(state), time.time(), service, ctype,
                   summary or f"Set {key} to {json.dumps(value)}", author,
                   _config_diff(service, key, old, value), key=key, old=old, new=value)


def set_fault(name: str, value, *, state: Path | None = None) -> None:
    state = Path(state) if state else state_dir()
    faults = read_json(state / "faults.json", {})
    faults[name] = value
    write_json(state / "faults.json", faults)


def enqueue_poison(*, state: Path | None = None) -> int:
    """A job whose payload is truncated JSON: every attempt to decode it fails."""
    state = Path(state) if state else state_dir()
    conn = db.connect(state / "app.db")
    try:
        cur = conn.execute(
            "INSERT INTO jobs(kind,payload,status,attempts,created_at) VALUES('send_receipt',?,'pending',0,?)",
            ('{"order_id": 48113, "customer": "cust-7", "email": "ana@exa', time.time()))
        return cur.lastrowid
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="testbed.deployctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    d = sub.add_parser("deploy"); d.add_argument("service"); d.add_argument("version"); d.add_argument("--rollback", action="store_true")
    c = sub.add_parser("config"); c.add_argument("service"); c.add_argument("key"); c.add_argument("value", help="JSON value")
    f = sub.add_parser("fault"); f.add_argument("name"); f.add_argument("value", help="JSON value")
    sub.add_parser("poison")
    a = p.parse_args()
    init_state()
    if a.cmd == "deploy":
        print(json.dumps(deploy(a.service, a.version, rollback=a.rollback), indent=2))
    elif a.cmd == "config":
        print(json.dumps(set_config(a.service, a.key, json.loads(a.value)), indent=2))
    elif a.cmd == "fault":
        set_fault(a.name, json.loads(a.value))
    elif a.cmd == "poison":
        print("enqueued job", enqueue_poison())


if __name__ == "__main__":
    main()
