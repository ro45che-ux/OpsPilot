"""Order API: the primary service under investigation.

Behaviour switches (n_plus_one, coupon_strict, pricing_cache_unbounded) come from the release
manifest; the matching diffs live in testbed/releases/catalog.py. Config and flags are read live
from the same manifest, and environmental faults from faults.json.
"""
from __future__ import annotations

import contextvars
import functools
import json
import os
import random
import threading
import time
import traceback
import tracemalloc
import uuid
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from testbed.common import db
from testbed.common.logs import JsonLogger
from testbed.common.state import JsonFileView, state_dir
from testbed.deployctl import init_state

STATE = init_state(state_dir())
_release = JsonFileView(STATE / "release.json")
_faults = JsonFileView(STATE / "faults.json")
log = JsonLogger("api", lambda: _cur().get("version", "unknown"), STATE / "logs")


def _cur() -> dict:
    return (_release.get() or {}).get("api", {})


def behaviors() -> set[str]:
    return set(_cur().get("behaviors", []))


def cfg(key: str, default):
    return _cur().get("config", {}).get(key, default)


# ---- metrics ---------------------------------------------------------------------------------
REG = CollectorRegistry()
REQS = Counter("http_requests", "HTTP requests", ["route", "status"], registry=REG)
LAT = Histogram("http_request_duration_seconds", "HTTP latency", ["route"], registry=REG,
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
DBQ = Counter("db_queries", "Database round trips", registry=REG)
POOL_TIMEOUTS = Counter("db_pool_timeouts", "Timeouts acquiring a db connection", registry=REG)
PAY_LAT = Histogram("payments_request_duration_seconds", "Payments provider latency", registry=REG,
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))
PAY_REQS = Counter("payments_requests", "Payments provider calls", ["outcome"], registry=REG)
_proc = psutil.Process()
Gauge("process_resident_memory_bytes", "RSS", registry=REG).set_function(lambda: _proc.memory_info().rss)
# RSS is exported too, but macOS swaps/compresses idle pages so it is a noisy leak signal on dev
# machines. Traced heap is the OS-independent equivalent of a runtime heap-in-use metric.
tracemalloc.start()
Gauge("process_heap_bytes", "Python heap in use", registry=REG).set_function(lambda: tracemalloc.get_traced_memory()[0])
Gauge("process_cpu_percent", "CPU percent since last scrape", registry=REG).set_function(lambda: _proc.cpu_percent(None))
Gauge("db_pool_in_use", "Connections checked out", registry=REG).set_function(lambda: get_pool().in_use)
Gauge("db_pool_size", "Configured pool size", registry=REG).set_function(lambda: get_pool().size)

# ---- database pool (rebuilt when config changes, like a restart with new settings) -----------
_pool: db.Pool | None = None
_pool_key: tuple | None = None
_pool_lock = threading.Lock()


class _ReqStats:
    __slots__ = ("queries", "request_id")

    def __init__(self):
        self.queries = 0
        self.request_id = uuid.uuid4().hex[:12]


_req: contextvars.ContextVar[_ReqStats] = contextvars.ContextVar("req_stats")


def _count_query() -> None:
    DBQ.inc()
    s = _req.get(None)
    if s:
        s.queries += 1


def get_pool() -> db.Pool:
    global _pool, _pool_key
    key = (int(cfg("db_pool_size", 8)), float(cfg("db_pool_timeout_s", 1.0)))
    if key != _pool_key:
        with _pool_lock:
            if key != _pool_key:
                _pool = db.Pool(STATE / "app.db", key[0], key[1], on_query=_count_query)
                if _pool_key is not None:
                    log.info("db pool reconfigured", pool_size=key[0], pool_timeout_s=key[1])
                _pool_key = key
    return _pool


# ---- request observation ---------------------------------------------------------------------
def observed(route: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            stats = _ReqStats()
            _req.set(stats)
            start = time.perf_counter()
            status, err, stack = 200, None, None
            try:
                return fn(*a, **kw)
            except db.PoolTimeout as e:
                POOL_TIMEOUTS.inc()
                status, err = 503, f"PoolTimeout: {e}"
                raise HTTPException(503, "database connection pool exhausted")
            except PaymentTimeout as e:
                status, err = 502, f"PaymentTimeout: {e}"
                raise HTTPException(502, "payment provider timed out")
            except HTTPException as e:
                status = e.status_code
                raise
            except Exception as e:
                status, err = 500, f"{type(e).__name__}: {e}"
                stack = traceback.format_exc(limit=-3)
                raise HTTPException(500, "internal error")
            finally:
                dur = time.perf_counter() - start
                REQS.labels(route, str(status)).inc()
                LAT.labels(route).observe(dur)
                level = "ERROR" if status >= 500 else "INFO"
                msg = f"unhandled exception in {route}" if stack else ("request failed" if status >= 500 else "request")
                log.log(level, msg, route=route, status=status, duration_ms=round(dur * 1000, 1),
                        db_queries=stats.queries, request_id=stats.request_id, error=err, stack=stack)
                if random.random() < 0.02:
                    log.warning("deprecated header X-Legacy-Client present", route=route)
        return wrapper
    return deco


# ---- domain logic ----------------------------------------------------------------------------
PRICES = {sku: 5.0 + (i * 3) % 40 for i, sku in enumerate(db.SKUS)}
COUPONS = {"SAVE10": 0.10, "WELCOME5": 0.05, "VIP20": 0.20}
LEAK_BYTES_PER_QUOTE = 384 * 1024
LEAK_CAP_BYTES = 600 * 1024 * 1024
PRICING_ENGINE_CPU_S = 0.06
PAYMENTS_TIMEOUT_S = 2.0

_PRICE_CACHE: dict[str, bytes] = {}


class PaymentTimeout(Exception):
    pass


class ItemIn(BaseModel):
    sku: str
    qty: int = 1


class OrderIn(BaseModel):
    customer: str
    items: list[ItemIn]
    coupon: str | None = None


def _burn_cpu(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def quote_price(items: list[ItemIn], request_id: str) -> float:
    if cfg("flags.new_pricing_engine", False):
        _burn_cpu(PRICING_ENGINE_CPU_S)  # new engine re-evaluates every pricing rule in Python
    subtotal = sum(PRICES.get(i.sku, 10.0) * i.qty for i in items)
    if "pricing_cache_unbounded" in behaviors():
        if len(_PRICE_CACHE) * LEAK_BYTES_PER_QUOTE < LEAK_CAP_BYTES:
            _PRICE_CACHE[request_id] = os.urandom(LEAK_BYTES_PER_QUOTE)  # incompressible, so RSS reflects it everywhere
    elif _PRICE_CACHE:
        _PRICE_CACHE.clear()  # the process restarted on rollback; memory is released
    return subtotal


def apply_coupon(total: float, code: str | None) -> float:
    if not code:
        return total
    if "coupon_strict" in behaviors():
        pct = COUPONS[code]
    else:
        pct = COUPONS.get(code, 0.0)  # unknown or expired codes are ignored
    return round(total * (1 - pct), 2)


def charge(amount: float) -> None:
    extra = float((_faults.get() or {}).get("payments_latency_ms", 0)) / 1000
    latency = random.uniform(0.06, 0.10) + extra
    start = time.perf_counter()
    if latency > PAYMENTS_TIMEOUT_S:
        time.sleep(PAYMENTS_TIMEOUT_S)
        PAY_REQS.labels("timeout").inc()
        PAY_LAT.observe(PAYMENTS_TIMEOUT_S)
        log.error("payments provider call timed out", provider="payments", duration_ms=PAYMENTS_TIMEOUT_S * 1000)
        raise PaymentTimeout(f"no response within {PAYMENTS_TIMEOUT_S}s")
    time.sleep(latency)
    dur = time.perf_counter() - start
    PAY_REQS.labels("ok").inc()
    PAY_LAT.observe(dur)
    if dur > 0.5:
        log.warning("payments provider slow", provider="payments", duration_ms=round(dur * 1000, 1))


def _serialize(order, items) -> dict:
    return {"id": order["id"], "customer": order["customer"], "total": order["total"],
            "items": [{"sku": i["sku"], "qty": i["qty"], "price": i["price"]} for i in items]}


# ---- routes ----------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("service started", pool_size=get_pool().size, behaviors=sorted(behaviors()))
    yield


app = FastAPI(title="orders-api", lifespan=lifespan)


# async so probes and scrapes run on the event loop and are not starved when the request
# threadpool is saturated, which is exactly when you most need them
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": _cur().get("version")}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(REG), media_type=CONTENT_TYPE_LATEST)


@app.get("/orders")
@observed("list_orders")
def list_orders(limit: int = 30):
    with get_pool().acquire() as conn:
        rows = conn.query("SELECT id, customer, total FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        if "n_plus_one" in behaviors():
            out = []
            for r in rows:
                items = conn.query("SELECT sku, qty, price FROM order_items WHERE order_id = ?", (r["id"],))
                out.append(_serialize(r, items))
            return out
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        items = conn.query(f"SELECT order_id, sku, qty, price FROM order_items WHERE order_id IN ({marks})", ids)
        by_order: dict[int, list] = {}
        for i in items:
            by_order.setdefault(i["order_id"], []).append(i)
        return [_serialize(r, by_order.get(r["id"], [])) for r in rows]


@app.post("/orders")
@observed("create_order")
def create_order(body: OrderIn):
    rid = _req.get().request_id
    subtotal = quote_price(body.items, rid)
    total = apply_coupon(subtotal, body.coupon)
    with get_pool().acquire() as conn:
        conn.query("SELECT 1")  # inventory reservation check
        charge(total)  # NOTE: the connection stays checked out while we wait on payments
        now = time.time()
        order_id = conn.write([("INSERT INTO orders(customer,total,created_at) VALUES(?,?,?)", (body.customer, total, now))])
        conn.write(
            [("INSERT INTO order_items(order_id,sku,qty,price) VALUES(?,?,?,?)", (order_id, i.sku, i.qty, PRICES.get(i.sku, 10.0)))
             for i in body.items]
            + [("INSERT INTO jobs(kind,payload,status,attempts,created_at) VALUES('send_receipt',?,'pending',0,?)",
                (json.dumps({"order_id": order_id, "customer": body.customer}), now))]
        )
    return {"id": order_id, "total": total}
