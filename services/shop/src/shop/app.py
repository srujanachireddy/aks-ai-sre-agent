"""One codebase, three services. SERVICE_NAME decides which role this process plays.

  checkout  POST /checkout   -> asks catalog for the price, then asks payments to charge
  catalog   GET  /price      -> returns a price
  payments  POST /charge     -> pretends to charge a card

Every service exposes:
  /healthz   liveness: "the process is alive"
  /readyz    readiness: "send me traffic" (payments says no when it is broken)
  /metrics   Prometheus metrics: request count, errors, latency, leaked memory
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .settings import Settings, load

PRICES = {"book": 12.5, "mug": 8.0, "shirt": 20.0}
RATES = {"USD": 1.0, "EUR": 0.92, "INR": 83.2}

# --------------------------------------------------------------------------- metrics
# These names are exactly what the agent's LiveBackend queries.
REQUESTS = Counter("app_requests_total", "Requests handled", ["service", "route", "status"])
LATENCY = Histogram("app_request_duration_seconds", "Request duration", ["service", "route"],
                    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
LEAKED = Gauge("app_leaked_bytes", "Memory held by the memory-leak fault", ["service"])
INFO = Gauge("app_info", "Running version", ["service", "version"])


# --------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy for Loki to search, easy for humans to read."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                 "service": getattr(record, "service", ""), "msg": record.getMessage()}
        if record.exc_info:
            entry["error"] = "".join(traceback.format_exception(*record.exc_info)).strip().splitlines()[-1]
            entry["trace"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry)


def make_logger(service: str) -> logging.LoggerAdapter:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    base = logging.getLogger(f"shop.{service}")
    base.handlers[:] = [handler]
    base.setLevel(logging.INFO)
    base.propagate = False
    return logging.LoggerAdapter(base, {"service": service})


# --------------------------------------------------------------------------- memory leak fault
class Leak:
    """Grabs memory every second and never gives it back, like a cache with no eviction."""

    def __init__(self, service: str, mb_per_minute: float):
        self.service, self.rate = service, mb_per_minute
        self.chunks: list[bytearray] = []
        self.stop = threading.Event()

    def start(self) -> None:
        if self.rate > 0:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        per_second = int(self.rate * 1024 * 1024 / 60)
        while not self.stop.wait(1.0):
            self.chunks.append(bytearray(b"x" * per_second))  # touch the bytes so they really count
            LEAKED.labels(self.service).set(sum(len(c) for c in self.chunks))


def burn_cpu(ms: float) -> None:
    end = time.perf_counter() + ms / 1000
    x = 0
    while time.perf_counter() < end:
        x += 1


# --------------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    s = settings or load()
    log = make_logger(s.service)
    leak = Leak(s.service, s.leak_mb_per_minute)
    client = httpx.Client(timeout=s.downstream_timeout_s, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        INFO.labels(s.service, s.version).set(1)
        leak.start()
        log.info(f"starting {s.service} {s.version}")
        yield
        leak.stop.set()
        client.close()

    app = FastAPI(title=f"shop-{s.service}", lifespan=lifespan)
    app.state.settings, app.state.leak = s, leak

    @app.middleware("http")
    async def measure(request: Request, call_next):
        route = request.url.path
        if route in ("/metrics", "/healthz", "/readyz"):
            return await call_next(request)
        start = time.perf_counter()
        if s.extra_latency_ms:
            await asyncio.sleep(s.extra_latency_ms / 1000)  # slows this request only
        if s.cpu_work_ms:
            burn_cpu(s.cpu_work_ms)  # deliberately blocking: real CPU saturation
        try:
            if s.error_rate and random.random() < s.error_rate:
                raise RuntimeError("injected error (FAULT_ERROR_RATE)")
            response = await call_next(request)
        except Exception:
            log.exception(f"unhandled exception in {request.method} {route}")
            response = JSONResponse({"error": "internal error"}, status_code=500)
        REQUESTS.labels(s.service, route, str(response.status_code)).inc()
        LATENCY.labels(s.service, route).observe(time.perf_counter() - start)
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive", "version": s.version}

    @app.get("/readyz")
    def readyz() -> Response:
        if s.fail_all_requests:
            return JSONResponse({"status": "not ready", "reason": "FAULT_FAIL_ALL_REQUESTS"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---- catalog
    if s.service == "catalog":
        @app.get("/price")
        def price(item: str = "book") -> dict:
            if item not in PRICES:
                return JSONResponse({"error": f"unknown item {item}"}, status_code=404)
            return {"item": item, "price_usd": PRICES[item]}

    # ---- payments
    if s.service == "payments":
        @app.post("/charge")
        def charge(body: dict) -> dict:
            if s.fail_all_requests:
                raise RuntimeError("payment processor configuration invalid (FAULT_FAIL_ALL_REQUESTS)")
            return {"charged": body.get("amount", 0), "currency": body.get("currency", "USD"), "status": "approved"}

    # ---- checkout
    if s.service == "checkout":
        def apply_currency(cart: dict, amount_usd: float) -> tuple[float, str]:
            if s.bug_currency_keyerror:
                # The "v1.4.0 bug": assumes every cart has a currency field. Old clients don't send one.
                rate = RATES[cart["currency"]]
                return round(amount_usd * rate, 2), cart["currency"]
            currency = cart.get("currency", "USD")
            return round(amount_usd * RATES.get(currency, 1.0), 2), currency

        @app.post("/checkout")
        def checkout(cart: dict) -> Response:
            item = cart.get("item", "book")
            try:
                p = client.get(f"{s.catalog_url}/price", params={"item": item})
                p.raise_for_status()
            except httpx.HTTPError as exc:
                log.error(f"catalog call failed: GET {s.catalog_url}/price failed: {exc}")
                return JSONResponse({"error": "catalog unavailable"}, status_code=502)

            amount, currency = apply_currency(cart, p.json()["price_usd"] * int(cart.get("quantity", 1)))

            try:
                c = client.post(f"{s.payments_url}/charge", json={"amount": amount, "currency": currency})
                c.raise_for_status()
            except httpx.HTTPError as exc:
                log.error(f"payments call failed: POST {s.payments_url}/charge failed: {exc}")
                return JSONResponse({"error": "payment step failed"}, status_code=502)

            return JSONResponse({"status": "ordered", "item": item, "amount": amount, "currency": currency})

    return app


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=False)


if __name__ == "__main__":
    main()
