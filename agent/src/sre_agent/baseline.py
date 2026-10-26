"""A rule-based triage baseline: the 'traditional automation' the agent is compared against.

It uses the same tools and the same evidence, but fixed if/else rules in a fixed order.
It is fast, free and predictable, and it fails exactly where judgment is needed
(for example, blaming a deploy that happened after the problem had already started).
"""

from __future__ import annotations

import re

from .models import ActionType, Alert, Category, Diagnosis, ProposedAction
from .tools import ToolBackend

_DEP_PATTERNS = re.compile(r"(?i)(connection refused|no such host|i/o timeout|upstream connect error|dial tcp|503 from)")


def _previous_tag(history: str) -> str | None:
    tags = re.findall(r"tag ([A-Za-z0-9_.\-]+)", history)
    return tags[1] if len(tags) > 1 else None


def triage(alert: Alert, backend: ToolBackend) -> Diagnosis:
    metrics = backend.get_service_metrics(alert.service)
    logs = backend.get_recent_logs(alert.service)
    events = backend.get_kubernetes_events(alert.namespace)
    history = backend.get_deploy_history(alert.service)

    def make(category: Category, cause: str, action: ProposedAction, evidence: list[str], conf: float) -> Diagnosis:
        return Diagnosis(service=alert.service, category=category, root_cause=cause, confidence=conf,
                         evidence=evidence, action=action, source="baseline")

    # Rule 1: OOMKilled anywhere for this service -> memory
    if re.search(rf"(?i){re.escape(alert.service)}\S*.*OOMKill", events):
        return make(Category.MEMORY_EXHAUSTION, "Containers are being OOMKilled.",
                    ProposedAction(type=ActionType.ESCALATE, rationale="Rule cannot choose a safe new memory limit."),
                    ["OOMKilled events found"], 0.7)

    # Rule 2: a deploy in the last 30 minutes -> blame it and roll back
    if re.search(r"rolled out .* \((\d|[12]\d) min ago\)", history):
        prev = _previous_tag(history)
        return make(Category.BAD_DEPLOY, "A deploy happened in the last 30 minutes.",
                    ProposedAction(type=ActionType.ROLLBACK, target_image_tag=prev, rationale="Most recent change."),
                    ["recent deploy in history"], 0.6)

    # Rule 3: connection errors in logs -> dependency
    if _DEP_PATTERNS.search(logs):
        return make(Category.DEPENDENCY_FAILURE, "Logs show connection errors to a downstream service.",
                    ProposedAction(type=ActionType.ESCALATE, rationale="Downstream failure."),
                    ["connection errors in logs"], 0.6)

    # Rule 4: CPU near limit -> capacity
    if re.search(r"CPU used / limit:.*-> \d{2}:\d{2} (0\.9\d*|1(\.\d+)?)\s*$", metrics, re.M):
        return make(Category.CAPACITY, "CPU is at its limit.",
                    ProposedAction(type=ActionType.ESCALATE, rationale="Rule cannot choose a replica count."),
                    ["CPU >= 90% of limit"], 0.6)

    return make(Category.UNKNOWN, "No rule matched.",
                ProposedAction(type=ActionType.ESCALATE, rationale="No rule matched."), [], 0.0)
