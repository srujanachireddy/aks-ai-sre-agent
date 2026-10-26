# Demo shop

Three small services that form an online shop. The AI SRE agent watches them, and we break them on purpose.

```
loadgen ──► checkout ──► catalog   (GET /price)
                    └──► payments  (POST /charge)
```

One codebase and one container image; `SERVICE_NAME` picks the role.

## Failure switches

| Switch (environment variable) | Breaks | Matches agent scenario |
|---|---|---|
| `BUG_CURRENCY_KEYERROR=true` (baked into image v1.4.0) | checkout crashes on carts without a currency | `bad_deploy` |
| `FAULT_LEAK_MB_PER_MINUTE=20` | memory grows until the pod is OOMKilled | `memory_exhaustion` |
| `FAULT_CPU_WORK_MS=40` + higher loadgen `RPS` | CPU saturates, latency climbs | `capacity` |
| `FAULT_FAIL_ALL_REQUESTS=true` on payments | payments goes unready; checkout returns 502 | `dependency_failure` |
| `FAULT_ERROR_RATE=0.2`, `FAULT_EXTRA_LATENCY_MS=300` | random errors / slowness | general testing |

## Signals the agent reads

- Metrics: `app_requests_total{service,route,status}`, `app_request_duration_seconds`, `app_leaked_bytes`, `app_info{version}`
- Logs: one JSON object per line on stdout, with `level`, `service`, `msg`, and `error` for exceptions
- Health: `/healthz` (alive), `/readyz` (ready for traffic)

## Run the tests

```bash
cd services/shop
pip install -e ".[dev]"
pytest
```
