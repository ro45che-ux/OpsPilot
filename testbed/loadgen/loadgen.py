"""Open-loop load generator (Poisson arrivals), so a slow server cannot throttle the offered load."""
from __future__ import annotations

import argparse
import asyncio
import random
import signal
import time
from collections import Counter

import httpx

VALID_COUPONS = ["SAVE10", "WELCOME5", "VIP20"]
EXPIRED_COUPONS = ["SUMMER23", "BLACKFRIDAY22", "EXPIRED10"]
SKUS = [f"SKU-{i:03d}" for i in range(1, 21)]


def make_order(rng: random.Random) -> dict:
    body = {"customer": f"cust-{rng.randint(0, 39)}",
            "items": [{"sku": rng.choice(SKUS), "qty": rng.randint(1, 3)} for _ in range(rng.randint(1, 4))]}
    if rng.random() < 0.3:
        body["coupon"] = rng.choice(VALID_COUPONS if rng.random() < 0.5 else EXPIRED_COUPONS)
    return body


async def run(url: str, rate: float, get_share: float, seed: int) -> Counter:
    rng = random.Random(seed)
    stats: Counter = Counter()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks: set[asyncio.Task] = set()

    async with httpx.AsyncClient(base_url=url, timeout=5.0, limits=httpx.Limits(max_connections=400)) as client:
        async def one(kind: str, body: dict | None):
            try:
                r = await (client.get("/orders") if kind == "GET" else client.post("/orders", json=body))
                stats[f"{kind} {r.status_code}"] += 1
            except httpx.HTTPError as e:
                stats[f"{kind} {type(e).__name__}"] += 1

        next_t = time.perf_counter()
        while not stop.is_set():
            kind = "GET" if rng.random() < get_share else "POST"
            t = asyncio.create_task(one(kind, make_order(rng) if kind == "POST" else None))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            next_t += rng.expovariate(rate)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, next_t - time.perf_counter()))
            except asyncio.TimeoutError:
                pass
        await asyncio.gather(*tasks, return_exceptions=True)
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--rate", type=float, default=40.0, help="requests per second")
    p.add_argument("--get-share", type=float, default=0.6)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    stats = asyncio.run(run(a.url, a.rate, a.get_share, a.seed))
    print(dict(stats))


if __name__ == "__main__":
    main()
