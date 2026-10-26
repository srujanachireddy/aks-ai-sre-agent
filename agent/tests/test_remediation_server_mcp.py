import asyncio
import time

import yaml
from fastapi.testclient import TestClient

from sre_agent.guardrails import check
from sre_agent.llm import ScriptedModel, ToolCall, Turn
from sre_agent.mcp_server import build_server
from sre_agent.models import ActionType, Category, Diagnosis, ProposedAction
from sre_agent.remediation import apply_action, current_settings
from sre_agent.server import create_app
from sre_agent.tools import FixtureBackend

VALUES = """\
image:
  repository: acr.example.io/checkout
  tag: v1.4.0
replicas: 3
resources:
  limits:
    cpu: 500m
    memory: 256Mi
"""


def test_current_settings_parses_values():
    assert current_settings(yaml.safe_load(VALUES)) == {"image_tag": "v1.4.0", "replicas": 3, "memory_limit_mi": 256}
    assert current_settings({"resources": {"limits": {"memory": "1Gi"}}})["memory_limit_mi"] == 1024


def test_apply_action_changes_only_the_owned_field():
    d = Diagnosis(service="checkout", category=Category.BAD_DEPLOY, root_cause="x", confidence=0.9,
                  action=ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v1.3.2"))
    guard = check(d, current_settings(yaml.safe_load(VALUES)), {"v1.4.0", "v1.3.2"})
    new = yaml.safe_load(apply_action(VALUES, guard))
    old = yaml.safe_load(VALUES)
    assert new["image"]["tag"] == "v1.3.2"
    new["image"]["tag"] = old["image"]["tag"]
    assert new == old  # nothing else changed


def test_webhook_investigates_and_renders_pr(scenarios):
    s = scenarios["bad_deploy"]
    submit = {"category": "bad_deploy", "root_cause": "v1.4.0 KeyError", "confidence": 0.9, "evidence": ["412 KeyError"],
              "action": {"type": "rollback", "target_image_tag": "v1.3.2", "rationale": "last healthy"}}
    factory = lambda: ScriptedModel([  # noqa: E731
        Turn(tool_calls=[ToolCall("1", "get_deploy_history", {"service": "checkout"})]),
        Turn(tool_calls=[ToolCall("2", "submit_diagnosis", submit)]),
    ])
    client = TestClient(create_app(FixtureBackend(s.responses), factory, repo=None))
    payload = {"alerts": [{"status": "firing", "fingerprint": "abc",
                           "labels": {"alertname": "CheckoutErrorBudgetBurn", "service": "checkout", "namespace": "demo"},
                           "annotations": {"summary": "5xx 31%"}, "startsAt": "2026-10-26T14:04:00Z"}]}

    accepted = client.post("/alertmanager", json=payload).json()["accepted"]
    assert len(accepted) == 1
    assert client.post("/alertmanager", json=payload).json()["accepted"] == []  # deduplicated

    incident = client.get(f"/incidents/{accepted[0]}").json()
    assert incident["status"] == "done"
    assert incident["diagnosis"]["category"] == "bad_deploy"
    assert incident["dry_run_pr"]["title"].startswith("sre-agent: rollback checkout")
    assert "The agent cannot merge" in incident["dry_run_pr"]["body"]
    assert "sre_agent_investigations_total" in client.get("/metrics").text


def test_mcp_server_exposes_read_only_tools(scenarios):
    s = scenarios["dependency_failure"]
    server = build_server(FixtureBackend(s.responses))

    async def go():
        tools = await server.list_tools()
        result = await server.call_tool("get_dependencies", {"service": "checkout"})
        return tools, result

    tools, result = asyncio.run(go())
    names = {t.name for t in tools}
    assert names == {"get_service_metrics", "get_recent_logs", "get_kubernetes_events", "get_deploy_history", "get_dependencies"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert "payments: ready pods 0" in str(result)
