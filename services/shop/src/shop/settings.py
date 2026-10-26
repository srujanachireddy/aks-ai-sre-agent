"""All settings come from environment variables, so Kubernetes (and GitOps) controls them.

The FAULT_* and BUG_* switches let us cause each incident type on purpose.
Each one matches a scenario in agent/evals/scenarios.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Which of the three services this process is: checkout, payments or catalog.
    service: str
    version: str
    payments_url: str
    catalog_url: str
    downstream_timeout_s: float

    # --- failure switches (all off by default) ---
    # bad_deploy: a code bug baked into a release (KeyError on the 'currency' field)
    bug_currency_keyerror: bool
    # memory_exhaustion: hold this many MB more memory every minute, never release it
    leak_mb_per_minute: float
    # capacity: burn this much CPU per request, so heavy traffic saturates the pods
    cpu_work_ms: float
    # dependency_failure: fail readiness and return 500s (used on payments)
    fail_all_requests: bool
    # extra random errors and delay, for general testing
    error_rate: float
    extra_latency_ms: float


def load() -> Settings:
    return Settings(
        service=os.getenv("SERVICE_NAME", "checkout"),
        version=os.getenv("APP_VERSION", "dev"),
        payments_url=os.getenv("PAYMENTS_URL", "http://payments:8080"),
        catalog_url=os.getenv("CATALOG_URL", "http://catalog:8080"),
        downstream_timeout_s=_float("DOWNSTREAM_TIMEOUT_S", 2.0),
        bug_currency_keyerror=_bool("BUG_CURRENCY_KEYERROR"),
        leak_mb_per_minute=_float("FAULT_LEAK_MB_PER_MINUTE", 0.0),
        cpu_work_ms=_float("FAULT_CPU_WORK_MS", 0.0),
        fail_all_requests=_bool("FAULT_FAIL_ALL_REQUESTS"),
        error_rate=min(max(_float("FAULT_ERROR_RATE", 0.0), 0.0), 1.0),
        extra_latency_ms=_float("FAULT_EXTRA_LATENCY_MS", 0.0),
    )
