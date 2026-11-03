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
from datetime import datetime, timezone

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


def test_hpa_alert_rejects_unsupported_scaling(scenarios, monkeypatch):
    from sre_agent import server as server_module

    scenario = scenarios["bad_deploy"]

    # Use known GitOps values without accessing GitHub.
    monkeypatch.setattr(
        server_module.GitHubRepo,
        "read_public",
        lambda *args, **kwargs: VALUES,
    )
    monkeypatch.setenv("GITOPS_REPO", "test/repository")

    responses = dict(scenario.responses)
    responses["get_hpa_evidence"] = {
        "checkout": {
            "service": "checkout",
            "namespace": "shop",
            "current_replicas": 3,
            "desired_replicas": 3,
            "max_replicas": 3,
            "cpu_current_percent": 62,
            "cpu_target_percent": 70,
            "scaling_active": "True",
            "scaling_limited": "False",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
    }

    submit = {
        "category": "capacity",
        "root_cause": "HPA reached maximum replicas",
        "confidence": 0.9,
        "evidence": ["HPA maximum replicas reached"],
        "action": {
            "type": "scale",
            "replicas": 5,
            "rationale": "Increase capacity",
        },
    }

    factory = lambda: ScriptedModel([
        Turn(tool_calls=[
            ToolCall("1", "submit_diagnosis", submit)
        ]),
    ])

    client = TestClient(
        create_app(FixtureBackend(responses), factory, repo=None)
    )

    payload = {
        "alerts": [{
            "status": "firing",
            "fingerprint": "hpa-guardrail-integration-001",
            "labels": {
                "alertname": "KubeHpaMaxedOut",
                "service": "monitoring-kube-state-metrics",
                "horizontalpodautoscaler": "checkout",
                "namespace": "shop",
            },
            "annotations": {
                "summary": "Synthetic checkout HPA alert"
            },
            "startsAt": "2026-11-02T19:00:00Z",
        }]
    }

    response = client.post("/alertmanager", json=payload)
    assert response.status_code == 200

    accepted = response.json()["accepted"]
    assert len(accepted) == 1

    incident = client.get(f"/incidents/{accepted[0]}").json()

    assert incident["status"] == "done"
    assert incident["alert"]["service"] == "checkout"
    assert incident["diagnosis"]["action"]["type"] == "scale"
    assert incident["final_action"] == "escalate"
    assert incident["pull_request"] is None

    assert any(
        "HPA is not currently scaling-limited" in check
        for check in incident["guardrails"]
    )



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

def test_hpa_alert_maps_to_correct_service(scenarios):
    s = scenarios["capacity"]

    submit = {
        "category": "capacity",
        "root_cause": "Checkout HPA requires investigation",
        "confidence": 0.7,
        "evidence": ["HPA checkout reached its configured maximum"],
        "action": {
            "type": "escalate",
            "rationale": "Verify HPA metrics before scaling",
        },
    }

    factory = lambda: ScriptedModel([
        Turn(tool_calls=[
            ToolCall("1", "submit_diagnosis", submit)
        ])
    ])

    client = TestClient(
        create_app(FixtureBackend(s.responses), factory, repo=None)
    )

    payload = {
        "alerts": [{
            "status": "firing",
            "fingerprint": "hpa-checkout-test",
            "labels": {
                "alertname": "KubeHpaMaxedOut",
                "service": "monitoring-kube-state-metrics",
                "horizontalpodautoscaler": "checkout",
                "namespace": "shop",
            },
            "annotations": {
                "summary": "Checkout HPA reached maximum replicas"
            },
            "startsAt": "2026-11-02T17:22:45Z",
        }]
    }

    response = client.post("/alertmanager", json=payload)

    assert response.status_code == 200

    accepted = response.json()["accepted"]
    assert len(accepted) == 1

    incident = client.get(f"/incidents/{accepted[0]}").json()

    assert incident["alert"]["service"] == "checkout"