"""Policy checks between 'the model suggested X' and 'a pull request proposes X'.

The model is treated as untrusted input. Every proposed action is checked against
hard limits here, in plain code. If any check fails, the action is downgraded to
ESCALATE: a human gets the analysis, and no change is proposed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from .models import ActionType, Diagnosis, ProposedAction


@dataclass(frozen=True)
class Policy:
    managed_services: frozenset[str] = frozenset({"checkout", "payments", "catalog"})
    min_confidence: float = 0.6
    max_replicas: int = 10
    max_memory_mi: int = 1024
    max_memory_growth: float = 2.0  # never more than double a limit in one step


@dataclass
class GuardrailResult:
    allowed: bool
    action: ProposedAction
    checks: list[str] = field(default_factory=list)  # human-readable, goes into the PR body


_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")


def check(diagnosis: Diagnosis, current: dict, known_tags: set[str], policy: Policy = Policy(), hpa_evidence: dict | None = None,) -> GuardrailResult:
    """`current` holds the service's current GitOps values: image_tag, replicas, memory_limit_mi."""
    action = diagnosis.action
    checks: list[str] = []

    def deny(reason: str) -> GuardrailResult:
        checks.append(f"FAIL: {reason}")
        return GuardrailResult(
            allowed=False,
            action=ProposedAction(type=ActionType.ESCALATE, rationale=f"Escalated by guardrails: {reason}. {action.rationale}"),
            checks=checks,
        )

    if action.type == ActionType.ESCALATE:
        checks.append("PASS: no automated change proposed")
        return GuardrailResult(True, action, checks)

    if diagnosis.service not in policy.managed_services:
        return deny(f"service '{diagnosis.service}' is not on the managed list")
    checks.append(f"PASS: '{diagnosis.service}' is a managed service")

    if diagnosis.confidence < policy.min_confidence:
        return deny(f"confidence {diagnosis.confidence:.2f} is below {policy.min_confidence}")
    checks.append(f"PASS: confidence {diagnosis.confidence:.2f} >= {policy.min_confidence}")

    if action.type == ActionType.ROLLBACK:
        tag = action.target_image_tag or ""
        if not _TAG_RE.match(tag):
            return deny(f"rollback tag '{tag}' is not a valid image tag")
        if tag not in known_tags:
            return deny(f"rollback tag '{tag}' was never deployed (known: {sorted(known_tags)})")
        if tag == current.get("image_tag"):
            return deny(f"rollback tag '{tag}' is already the running version")
        checks.append(f"PASS: '{tag}' is a previously deployed version")


    elif action.type == ActionType.SCALE:
        n = action.replicas or 0

        if not 1 <= n <= policy.max_replicas:
            return deny(f"replicas {n} outside 1..{policy.max_replicas}")

        if n <= current.get("replicas", 0):
            return deny("proposed scaling does not increase capacity")

        if not hpa_evidence:
            return deny("HPA evidence unavailable")

        if hpa_evidence.get("service") != diagnosis.service:
            return deny("HPA evidence belongs to a different service")
        if hpa_evidence.get("namespace") != "shop":
            return deny("HPA evidence belongs to an unexpected namespace")

        collected_at = hpa_evidence.get("collected_at")

        if not isinstance(collected_at, str):
            return deny("HPA evidence timestamp unavailable")

        try:
            collected_time = datetime.fromisoformat(collected_at)
            if collected_time.tzinfo is None:
                return deny("HPA evidence timestamp has no timezone")
        except ValueError:
            return deny("HPA evidence timestamp is invalid")

        now = datetime.now(timezone.utc)
        age = now - collected_time

        if age < timedelta(0) or age > timedelta(minutes=2):
            return deny("HPA evidence is stale or timestamp is in the future")

        checks.append("PASS: HPA evidence is recent")


        cpu = hpa_evidence.get("cpu_current_percent")
        target = hpa_evidence.get("cpu_target_percent")

        if cpu is None or target is None:
            return deny("HPA CPU metrics unavailable")

        if hpa_evidence.get("scaling_active") != "True":
            return deny("HPA scaling metrics are not active")

        if hpa_evidence.get("scaling_limited") != "True":
            return deny("HPA is not currently scaling-limited")

        if cpu <= target:
            return deny("CPU utilization does not exceed HPA target")

        current_replicas = hpa_evidence.get("current_replicas")
        desired_replicas = hpa_evidence.get("desired_replicas")
        max_replicas = hpa_evidence.get("max_replicas")

        if not all(
            type(value) is int and value >= 1
            for value in (
                current_replicas,
                desired_replicas,
                max_replicas,
            )
        ):
            return deny("HPA replica evidence is missing or invalid")

        if not (
            current_replicas == desired_replicas == max_replicas
        ):
            return deny("HPA replica counts do not confirm maximum capacity")

        if max_replicas != current.get("replicas"):
            return deny("HPA maximum differs from current GitOps configuration")

        if n <= max_replicas:
            return deny("proposed HPA maximum does not increase capacity")

        checks.append(
            f"PASS: HPA at configured maximum of {max_replicas} replicas"
        )


        checks.append(
            f"PASS: HPA CPU {cpu}% exceeds target {target}%"
        )
        checks.append(
            f"PASS: replicas {current.get('replicas')} -> {n} "
            f"within limit {policy.max_replicas}"
        )


    elif action.type == ActionType.INCREASE_MEMORY:
        new, old = action.memory_limit_mi or 0, current.get("memory_limit_mi", 0)
        if new > policy.max_memory_mi:
            return deny(f"memory {new}Mi above cap {policy.max_memory_mi}Mi")
        if old and new > old * policy.max_memory_growth:
            return deny(f"memory {old}Mi -> {new}Mi is more than {policy.max_memory_growth}x in one step")
        if new <= old:
            return deny(f"memory {new}Mi is not an increase over {old}Mi")
        checks.append(f"PASS: memory {old}Mi -> {new}Mi within limits")

    return GuardrailResult(True, action, checks)
