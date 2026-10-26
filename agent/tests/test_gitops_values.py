"""The agent's fixes, applied to the REAL values files in gitops/apps. Each must be a one-line diff."""

import difflib
from pathlib import Path

import pytest
import yaml

from sre_agent.guardrails import check
from sre_agent.models import ActionType, Category, Diagnosis, ProposedAction
from sre_agent.remediation import apply_action, current_settings, set_yaml_value

APPS = Path(__file__).resolve().parents[2] / "gitops" / "apps"


def changed_lines(before: str, after: str) -> list[str]:
    return [line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def fix(service: str, action: ProposedAction, known_tags=("v1.3.2", "v1.4.0")) -> tuple[str, str]:
    before = (APPS / service / "values.yaml").read_text()
    current = current_settings(yaml.safe_load(before))
    d = Diagnosis(service=service, category=Category.BAD_DEPLOY, root_cause="x", confidence=0.9, action=action)
    guard = check(d, current, set(known_tags))
    assert guard.allowed, guard.checks
    return before, apply_action(before, guard)


def test_current_settings_from_real_checkout_values():
    values = yaml.safe_load((APPS / "checkout" / "values.yaml").read_text())
    assert current_settings(values) == {"image_tag": "v1.3.2", "replicas": 3, "memory_limit_mi": 256}


def test_rollback_is_a_one_line_diff_and_keeps_comments():
    before = (APPS / "checkout" / "values.yaml").read_text().replace("tag: v1.3.2", "tag: v1.4.0")
    d = Diagnosis(service="checkout", category=Category.BAD_DEPLOY, root_cause="x", confidence=0.9,
                  action=ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v1.3.2"))
    guard = check(d, current_settings(yaml.safe_load(before)), {"v1.3.2", "v1.4.0"})
    after = apply_action(before, guard)
    assert changed_lines(before, after) == ["-  tag: v1.4.0", "+  tag: v1.3.2"]
    assert after.startswith("# checkout:")  # header comment survived


def test_scale_raises_hpa_ceiling_not_replicas():
    before, after = fix("checkout", ProposedAction(type=ActionType.SCALE, replicas=6))
    assert changed_lines(before, after) == ["-  maxReplicas: 3", "+  maxReplicas: 6"]


def test_memory_changes_the_limit_not_the_request():
    before, after = fix("catalog", ProposedAction(type=ActionType.INCREASE_MEMORY, memory_limit_mi=512))
    assert changed_lines(before, after) == ["-    memory: 256Mi", "+    memory: 512Mi"]
    v = yaml.safe_load(after)
    assert v["resources"]["limits"]["memory"] == "512Mi" and v["resources"]["requests"]["memory"] == "96Mi"


def test_scale_without_autoscaling_sets_replicas():
    from sre_agent.guardrails import GuardrailResult

    before = (APPS / "loadgen" / "values.yaml").read_text()
    after = apply_action(before, GuardrailResult(True, ProposedAction(type=ActionType.SCALE, replicas=2)))
    assert changed_lines(before, after) == ["-replicas: 1", "+replicas: 2"]


def test_guardrails_refuse_to_touch_unmanaged_loadgen():
    d = Diagnosis(service="loadgen", category=Category.CAPACITY, root_cause="x", confidence=0.95,
                  action=ProposedAction(type=ActionType.SCALE, replicas=2))
    assert not check(d, {"replicas": 1}, set()).allowed


def test_set_yaml_value_keeps_inline_comment_and_rejects_missing_path():
    text = "image:\n  tag: v1 # pinned\n"
    assert set_yaml_value(text, ["image", "tag"], "v2") == "image:\n  tag: v2 # pinned\n"
    with pytest.raises(KeyError):
        set_yaml_value(text, ["image", "digest"], "x")
