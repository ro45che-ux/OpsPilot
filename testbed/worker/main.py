"""Receipt worker: drains the jobs table in id order.

Like the 2.3.0 release notes say, a failing job is retried forever with a 1s backoff, so one
poison message blocks the queue. On startup the worker quarantines jobs that already burned
through max_attempts, which is why a restart clears a poison pill.
"""
from __future__ import annotations

import json
import os
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from testbed.common import db
from testbed.common.logs import JsonLogger
from testbed.common.state import JsonFileView, state_dir
from testbed.deployctl import init_state

STATE = init_state(state_dir())
_release = JsonFileView(STATE / "release.json")


def _cur() -> dict:
    return (_release.get() or {}).get("worker", {})


log = JsonLogger("worker", lambda: _cur().get("version", "unknown"), STATE / "logs")
REG = CollectorRegistry()
PROCESSED = Counter("jobs_processed", "Jobs completed", registry=REG)
FAILED = Counter("jobs_failed", "Job attempts that raised", registry=REG)


def _scalar(sql: str, params=()):
    c = db.connect(STATE / "app.db")
    try:
        return c.execute(sql, params).fetchone()[0]
    except Exception:
        return 0
    finally:
        c.close()


Gauge("queue_depth", "Pending jobs", registry=REG).set_function(
    lambda: _scalar("SELECT COUNT(*) FROM jobs WHERE status='pending'"))
Gauge("queue_oldest_age_seconds", "Age of the oldest pending job", registry=REG).set_function(
    lambda: time.time() - (_scalar("SELECT MIN(created_at) FROM jobs WHERE status='pending'") or time.time()))


def quarantine(conn) -> None:
    max_attempts = int(_cur().get("config", {}).get("max_attempts", 5))
    rows = conn.execute("SELECT id FROM jobs WHERE status='pending' AND attempts >= ?", (max_attempts,)).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(f"UPDATE jobs SET status='dead' WHERE id IN ({','.join('?' * len(ids))})", ids)
        log.warning("quarantined jobs that exceeded max_attempts", job_ids=ids, max_attempts=max_attempts)


def handle(job) -> None:
    payload = json.loads(job["payload"])
    time.sleep(0.015)  # render + send the receipt
    _ = payload["order_id"]


def main() -> None:
    port = int(os.environ.get("WORKER_METRICS_PORT", "9101"))
    start_http_server(port, addr=os.environ.get("WORKER_METRICS_ADDR", "127.0.0.1"), registry=REG)
    conn = db.connect(STATE / "app.db")
    quarantine(conn)
    log.info("service started", metrics_port=port)
    while True:
        job = conn.execute("SELECT id, payload, attempts FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if job is None:
            time.sleep(0.05)
            continue
        try:
            handle(job)
            conn.execute("UPDATE jobs SET status='done', done_at=? WHERE id=?", (time.time(), job["id"]))
            PROCESSED.inc()
        except Exception as e:
            FAILED.inc()
            attempt = job["attempts"] + 1
            conn.execute("UPDATE jobs SET attempts=? WHERE id=?", (attempt, job["id"]))
            log.error("job failed, will retry", job_id=job["id"], attempt=attempt,
                      error=f"{type(e).__name__}: {e}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
