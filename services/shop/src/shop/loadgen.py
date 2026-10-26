"""Sends steady fake customer traffic to checkout, so there is always something to measure.

RPS controls how many requests per second. Raising it is how we simulate a traffic spike
(the "capacity" scenario). About 1 in 4 carts has no currency field, like older mobile apps,
which is what triggers the v1.4.0 bug.
"""

from __future__ import annotations

import os
import random
import threading
import time

import httpx

ITEMS = ["book", "mug", "shirt"]


def make_cart() -> dict:
    cart = {"item": random.choice(ITEMS), "quantity": random.randint(1, 3)}
    if random.random() < 0.75:
        cart["currency"] = random.choice(["USD", "EUR", "INR"])
    return cart


def main() -> None:
    target = os.getenv("TARGET_URL", "http://checkout:8080/checkout")
    rps = float(os.getenv("RPS", "10"))
    workers = int(os.getenv("WORKERS", "8"))
    print(f"loadgen: {rps} req/s to {target}", flush=True)
    stats = {"ok": 0, "fail": 0}
    lock = threading.Lock()

    def worker() -> None:
        with httpx.Client(timeout=5) as client:
            while True:
                start = time.perf_counter()
                try:
                    r = client.post(target, json=make_cart())
                    key = "ok" if r.status_code < 500 else "fail"
                except httpx.HTTPError:
                    key = "fail"
                with lock:
                    stats[key] += 1
                time.sleep(max(0.0, workers / rps - (time.perf_counter() - start)))

    for _ in range(workers):
        threading.Thread(target=worker, daemon=True).start()
    while True:
        time.sleep(30)
        with lock:
            print(f"loadgen: last 30s ok={stats['ok']} fail={stats['fail']}", flush=True)
            stats.update(ok=0, fail=0)


if __name__ == "__main__":
    main()
