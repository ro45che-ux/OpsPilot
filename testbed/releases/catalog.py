"""Release catalog for the simulated deploy system.

A 'release' is a named set of behaviours the service code switches on (see testbed/api/app.py),
plus the commit metadata and unified diff an investigator would read. The diffs are authored to
match the behaviour switches in the code: the agent only ever sees the diff, never the switch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    service: str
    version: str
    summary: str
    author: str
    behaviors: tuple[str, ...] = ()
    diff: str = ""


BASELINE = {
    "api": {
        "version": "1.4.1",
        "config": {"db_pool_size": 8, "db_pool_timeout_s": 1.0, "flags.new_pricing_engine": False},
    },
    "worker": {"version": "2.3.0", "config": {"max_attempts": 5}},
}

# Changes already in history when an incident starts: (seconds before baseline, kind, args)
HISTORY = [
    (6 * 86400, "deploy", ("api", "1.3.9")),
    (5 * 86400, "deploy", ("worker", "2.2.0")),
    (4 * 86400, "config", ("api", "db_pool_size", 4, 8, "Raise DB pool from 4 to 8 after connection waits at peak", "ops.sam")),
    (3 * 86400, "deploy", ("api", "1.4.0")),
    (2 * 86400, "deploy", ("worker", "2.3.0")),
    (26 * 3600, "deploy", ("api", "1.4.1")),
]

_RELEASES = [
    Release("api", "1.3.9", "Bump fastapi to 0.115.4", "dev.lena", diff="""\
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,3 @@
-fastapi==0.115.2
+fastapi==0.115.4
 uvicorn==0.32.0
 prometheus-client==0.21.0
"""),
    Release("api", "1.4.0", "Add /health endpoint for load balancer checks", "dev.raj", diff="""\
--- a/testbed/api/app.py
+++ b/testbed/api/app.py
@@ -40,3 +40,7 @@
+@app.get("/health")
+def health():
+    return {"status": "ok", "version": version()}
+
"""),
    Release("api", "1.4.1", "Log request_id on every request line", "dev.raj", diff="""\
--- a/testbed/api/app.py
+++ b/testbed/api/app.py
@@ -88,6 +88,7 @@ def wrapper(*a, **kw):
-            log.log(level, "request", route=route, status=status)
+            log.log(level, "request", route=route, status=status, request_id=rid)
"""),
    Release("api", "1.5.0", "orders: load line items per order to keep list payload small", "dev.maya",
            behaviors=("n_plus_one",), diff="""\
--- a/testbed/api/app.py
+++ b/testbed/api/app.py
@@ -112,12 +112,12 @@ def list_orders(limit: int = 30):
     with get_pool().acquire() as conn:
         rows = conn.query("SELECT id, customer, total FROM orders ORDER BY id DESC LIMIT ?", (limit,))
-        ids = [r["id"] for r in rows]
-        marks = ",".join("?" * len(ids))
-        items = conn.query(f"SELECT order_id, sku, qty, price FROM order_items WHERE order_id IN ({marks})", ids)
-        by_order = group_by(items, "order_id")
-        return [serialize(r, by_order.get(r["id"], [])) for r in rows]
+        out = []
+        for r in rows:
+            items = conn.query("SELECT sku, qty, price FROM order_items WHERE order_id = ?", (r["id"],))
+            out.append(serialize(r, items))
+        return out
"""),
    Release("api", "1.5.1", "checkout: apply coupon discounts from the COUPONS table", "dev.raj",
            behaviors=("coupon_strict",), diff="""\
--- a/testbed/api/app.py
+++ b/testbed/api/app.py
@@ -74,7 +74,7 @@ def apply_coupon(total: float, code: str | None) -> float:
     if not code:
         return total
-    pct = COUPONS.get(code, 0.0)  # unknown or expired codes are ignored
+    pct = COUPONS[code]
     return round(total * (1 - pct), 2)
"""),
    Release("api", "1.5.2", "pricing: memoize price quotes", "dev.maya",
            behaviors=("pricing_cache_unbounded",), diff="""\
--- a/testbed/api/app.py
+++ b/testbed/api/app.py
@@ -58,10 +58,14 @@
+_PRICE_CACHE: dict[str, bytes] = {}
+
 def quote_price(items, request_id: str) -> float:
     subtotal = sum(PRICES.get(i.sku, 10.0) * i.qty for i in items)
+    # keep the serialized quote around so repeat lookups are cheap
+    _PRICE_CACHE[request_id] = serialize_quote(items, subtotal)
     return subtotal
"""),
    Release("api", "1.5.3", "email: update receipt copy and footer links", "dev.lena", diff="""\
--- a/templates/receipt_email.html
+++ b/templates/receipt_email.html
@@ -12,5 +12,5 @@
-  <p>Thanks for your order! Questions? Email help@shop.example</p>
+  <p>Thank you for shopping with us! Need help? Visit our Help Center.</p>
-  <a href="https://shop.example/terms-2023">Terms</a>
+  <a href="https://shop.example/terms">Terms</a>
"""),
    Release("worker", "2.2.0", "Process receipt jobs in id order", "dev.sam", diff="""\
--- a/testbed/worker/main.py
+++ b/testbed/worker/main.py
@@ -30,2 +30,2 @@
-    "SELECT ... FROM jobs WHERE status='pending'"
+    "SELECT ... FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
"""),
    Release("worker", "2.3.0", "worker: retry failed jobs with 1s backoff", "dev.sam", diff="""\
--- a/testbed/worker/main.py
+++ b/testbed/worker/main.py
@@ -52,7 +52,8 @@
         except Exception as e:
             bump_attempts(job)
-            mark_failed(job)
+            log.error("job failed, will retry", job_id=job.id, attempt=job.attempts)
+            time.sleep(1.0)
"""),
    Release("worker", "2.3.1", "worker: reword log messages for consistency", "dev.lena", diff="""\
--- a/testbed/worker/main.py
+++ b/testbed/worker/main.py
@@ -61,1 +61,1 @@
-    log.info("job ok")
+    log.info("job processed")
"""),
]

RELEASES: dict[tuple[str, str], Release] = {(r.service, r.version): r for r in _RELEASES}
