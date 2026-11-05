"""SQLite-backed 'database' with a bounded connection pool.

SQLite is local, so each round trip sleeps for a simulated network RTT. That makes query count
(N+1) and connection hold time (pool exhaustion) behave like they would against a remote DB.
"""
from __future__ import annotations

import queue
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL, total REAL NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS order_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, done_at REAL);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
"""

SKUS = [f"SKU-{i:03d}" for i in range(1, 21)]
RTT_S = 0.006


class PoolTimeout(Exception):
    pass


def init_db(path: Path, seed_orders: int = 300) -> None:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            rng = random.Random(7)
            now = time.time() - 86400
            for i in range(seed_orders):
                items = [(rng.choice(SKUS), rng.randint(1, 3), round(rng.uniform(5, 60), 2)) for _ in range(rng.randint(1, 5))]
                total = round(sum(q * p for _, q, p in items), 2)
                cur = conn.execute("INSERT INTO orders(customer,total,created_at) VALUES(?,?,?)", (f"cust-{i % 40}", total, now + i * 60))
                conn.executemany("INSERT INTO order_items(order_id,sku,qty,price) VALUES(?,?,?,?)", [(cur.lastrowid, *it) for it in items])
        conn.execute("COMMIT")
    finally:
        conn.close()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class Conn:
    def __init__(self, path: Path, on_query: Callable[[], None] | None):
        self._c = connect(path)
        self._on_query = on_query

    def _round_trip(self) -> None:
        time.sleep(RTT_S)
        if self._on_query:
            self._on_query()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        self._round_trip()
        return self._c.execute(sql, params).fetchall()

    def write(self, statements: list[tuple[str, tuple]]) -> int | None:
        """Run several statements as one transaction in a single round trip. Returns last rowid."""
        self._round_trip()
        self._c.execute("BEGIN IMMEDIATE")
        try:
            last = None
            for sql, params in statements:
                last = self._c.execute(sql, params).lastrowid
            self._c.execute("COMMIT")
            return last
        except Exception:
            self._c.execute("ROLLBACK")
            raise


class Pool:
    def __init__(self, path: Path, size: int, timeout_s: float, on_query: Callable[[], None] | None = None):
        self.size = size
        self.timeout_s = timeout_s
        self.in_use = 0
        self._lock = threading.Lock()
        self._free: queue.LifoQueue[Conn] = queue.LifoQueue()
        for _ in range(size):
            self._free.put(Conn(path, on_query))

    @contextmanager
    def acquire(self):
        try:
            conn = self._free.get(timeout=self.timeout_s)
        except queue.Empty:
            raise PoolTimeout(
                f"timed out after {self.timeout_s}s waiting for a database connection "
                f"(pool_size={self.size}, in_use={self.in_use})"
            ) from None
        with self._lock:
            self.in_use += 1
        try:
            yield conn
        finally:
            with self._lock:
                self.in_use -= 1
            self._free.put(conn)
