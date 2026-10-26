"""Data shapes shared by the agent, the baseline, guardrails and remediation."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Category(str, Enum):
    """What kind of failure this is. Every diagnosis must pick exactly one."""

    BAD_DEPLOY = "bad_deploy"  # a new version introduced the failure
    MEMORY_EXHAUSTION = "memory_exhaustion"  # OOMKills, memory at limit
    CAPACITY = "capacity"  # load beyond what current replicas/CPU can serve
    DEPENDENCY_FAILURE = "dependency_failure"  # a downstream service or datastore is failing
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    """The only changes the agent may propose. Anything else is a human's job."""

    ROLLBACK = "rollback"  # set image tag back to a previous known-good version
    SCALE = "scale"  # change replica count
    INCREASE_MEMORY = "increase_memory"  # raise the container memory limit
    ESCALATE = "escalate"  # no safe automated fix: page a human with the analysis


class Alert(BaseModel):
    name: str
    service: str
    namespace: str = "demo"
    severity: str = "warning"
    summary: str = ""
    starts_at: str = ""
    fingerprint: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class ProposedAction(BaseModel):
    type: ActionType
    target_image_tag: str | None = None  # for ROLLBACK
    replicas: int | None = None  # for SCALE
    memory_limit_mi: int | None = None  # for INCREASE_MEMORY
    rationale: str = ""


class Diagnosis(BaseModel):
    service: str
    category: Category
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    action: ProposedAction
    source: str = "agent"  # "agent" or "baseline"


class ToolCallRecord(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result_chars: int


class Investigation(BaseModel):
    """Everything about one investigation, kept for audit and evaluation."""

    alert: Alert
    diagnosis: Diagnosis | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
