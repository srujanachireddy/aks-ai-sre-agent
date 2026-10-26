"""Each test proves one behaviour the AI agent depends on: the right metrics, logs and failures."""

import dataclasses
import time

import httpx
from fastapi.testclient import TestClient

from shop.app import create_app
from shop.settings import load


def settings(**overrides):
    return dataclasses.replace(load(), **overrides)


def fake_downstream(payments_status: int = 200, catalog_ok: bool = True, payments_down: bool = False):
    """Pretends to be catalog and payments, so checkout can be tested on its own."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/price":
            return httpx.Response(200, json={"item": "book", "price_usd": 12.5}) if catalog_ok else httpx.Response(503)
        if request.url.path == "/charge":
            if payments_down:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(payments_status, json={"status": "approved"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def checkout_client(transport=None, **overrides) -> TestClient:
    return TestClient(create_app(settings(service="checkout", **overrides), transport or fake_downstream()))


def metric(client: TestClient, line_start: str) -> float:
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(line_start):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# ---- healthy behaviour


def test_checkout_happy_path_with_and_without_currency():
    with checkout_client() as c:
        r = c.post("/checkout", json={"item": "book", "quantity": 2, "currency": "EUR"})
        assert r.status_code == 200 and r.json()["amount"] == 23.0 and r.json()["currency"] == "EUR"
        r = c.post("/checkout", json={"item": "book"})  # old clients send no currency
        assert r.status_code == 200 and r.json()["currency"] == "USD"


def test_metrics_count_requests_by_status():
    with checkout_client() as c:
        before = metric(c, 'app_requests_total{route="/checkout",service="checkout",status="200"}')
        c.post("/checkout", json={"item": "mug", "currency": "USD"})
        after = metric(c, 'app_requests_total{route="/checkout",service="checkout",status="200"}')
        assert after == before + 1
        assert "app_request_duration_seconds_bucket" in c.get("/metrics").text


def test_catalog_and_payments_roles():
    with TestClient(create_app(settings(service="catalog"))) as cat:
        assert cat.get("/price", params={"item": "shirt"}).json()["price_usd"] == 20.0
        assert cat.get("/price", params={"item": "car"}).status_code == 404
    with TestClient(create_app(settings(service="payments"))) as pay:
        assert pay.post("/charge", json={"amount": 5}).json()["status"] == "approved"
        assert pay.get("/readyz").status_code == 200


# ---- each failure switch matches one agent scenario


def test_bad_deploy_bug_raises_keyerror_only_without_currency(capsys):
    with checkout_client(bug_currency_keyerror=True) as c:
        assert c.post("/checkout", json={"item": "book", "currency": "USD"}).status_code == 200
        assert c.post("/checkout", json={"item": "book"}).status_code == 500
    logs = capsys.readouterr().out
    assert "KeyError: 'currency'" in logs and '"level": "ERROR"' in logs


def test_dependency_failure_payments_unready_and_checkout_logs_connection_error(capsys):
    with TestClient(create_app(settings(service="payments", fail_all_requests=True))) as pay:
        assert pay.get("/readyz").status_code == 503
        assert pay.post("/charge", json={"amount": 5}).status_code == 500
    with checkout_client(fake_downstream(payments_down=True)) as c:
        assert c.post("/checkout", json={"item": "book", "currency": "USD"}).status_code == 502
    assert "payments call failed" in capsys.readouterr().out


def test_capacity_cpu_work_slows_each_request():
    with checkout_client(cpu_work_ms=60) as c:
        start = time.perf_counter()
        c.post("/checkout", json={"item": "book", "currency": "USD"})
        assert time.perf_counter() - start >= 0.06


def test_memory_leak_grows_over_time():
    app = create_app(settings(service="catalog", leak_mb_per_minute=60))  # 1 MB per second
    with TestClient(app) as c:
        time.sleep(2.5)
        leaked = metric(c, 'app_leaked_bytes{service="catalog"}')
    assert leaked >= 2 * 1024 * 1024


def test_injected_error_rate():
    with checkout_client(error_rate=1.0) as c:
        assert c.post("/checkout", json={"item": "book", "currency": "USD"}).status_code == 500
        assert c.get("/healthz").status_code == 200  # health checks are never broken by this switch
