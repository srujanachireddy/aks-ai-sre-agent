from sre_agent.guardrails import Policy, check
from sre_agent.models import ActionType, Category, Diagnosis, ProposedAction
from sre_agent.tools import FixtureBackend, call_tool, redact
from datetime import datetime, timezone, timedelta

CURRENT = {"image_tag": "v1.4.0", "replicas": 3, "memory_limit_mi": 256}
KNOWN = {"v1.4.0", "v1.3.2", "v1.3.1"}


def diag(action: ProposedAction, confidence: float = 0.9, service: str = "checkout") -> Diagnosis:
    return Diagnosis(service=service, category=Category.BAD_DEPLOY, root_cause="x", confidence=confidence, action=action)


# ---- tools


def test_redact_removes_credentials():
    text = "conn failed password=hunter2 Authorization: Bearer abc.def.ghi AccountKey=Zm9v;x"
    out = redact(text)
    assert "hunter2" not in out and "abc.def" not in out and "Zm9v" not in out
    assert "[REDACTED]" in out


def test_call_tool_rejects_unknown_tool_and_bad_args():
    backend = FixtureBackend({})
    assert call_tool(backend, "delete_namespace", {"namespace": "demo"}).startswith("ERROR: unknown tool")
    assert call_tool(backend, "get_recent_logs", {"svc": "x"}).startswith("ERROR: bad arguments")


def test_call_tool_clips_huge_output():
    backend = FixtureBackend({"get_recent_logs": {"checkout": "x" * 50_000}})
    out = call_tool(backend, "get_recent_logs", {"service": "checkout"})
    assert len(out) < 4200 and "truncated" in out


def test_call_tool_survives_backend_exceptions():
    class Broken(FixtureBackend):
        def get_service_metrics(self, service, minutes=30):
            raise ConnectionError("prometheus down")

    out = call_tool(Broken({}), "get_service_metrics", {"service": "checkout"})
    assert out.startswith("ERROR: get_service_metrics failed: ConnectionError")


# ---- guardrails


def test_valid_rollback_is_allowed():
    r = check(diag(ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v1.3.2")), CURRENT, KNOWN)
    assert r.allowed and r.action.type == ActionType.ROLLBACK


def test_rollback_to_never_deployed_tag_is_escalated():
    r = check(diag(ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v9.9.9")), CURRENT, KNOWN)
    assert not r.allowed and r.action.type == ActionType.ESCALATE


def test_rollback_tag_injection_is_rejected():
    r = check(diag(ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v1; rm -rf /")), CURRENT, KNOWN)
    assert not r.allowed


def test_low_confidence_is_escalated():
    r = check(diag(ProposedAction(type=ActionType.ROLLBACK, target_image_tag="v1.3.2"), confidence=0.4), CURRENT, KNOWN)
    assert not r.allowed


def test_unmanaged_service_is_escalated():
    r = check(diag(ProposedAction(type=ActionType.SCALE, replicas=5), service="kube-dns"), CURRENT, KNOWN)
    assert not r.allowed



def test_scale_limits():
    evidence = {
        "service": "checkout",
        "namespace": "shop",
        "current_replicas": 3,
        "desired_replicas": 3,
        "max_replicas": 3,
        "cpu_current_percent": 95,
        "cpu_target_percent": 70,
        "scaling_active": "True",
        "scaling_limited": "True",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    def evaluate(replicas):
        diagnosis = diag(
            ProposedAction(
                type=ActionType.SCALE,
                replicas=replicas,
            )
        )
        return check(
            diagnosis,
            CURRENT,
            KNOWN,
            hpa_evidence=evidence,
        )

    assert evaluate(6).allowed
    assert not evaluate(50).allowed
    assert not evaluate(2).allowed



def test_memory_limits():
    assert check(diag(ProposedAction(type=ActionType.INCREASE_MEMORY, memory_limit_mi=512)), CURRENT, KNOWN).allowed
    assert not check(diag(ProposedAction(type=ActionType.INCREASE_MEMORY, memory_limit_mi=1024)), CURRENT, KNOWN).allowed
    assert not check(diag(ProposedAction(type=ActionType.INCREASE_MEMORY, memory_limit_mi=4096)), CURRENT, KNOWN, Policy()).allowed

def test_live_backend_memory_limit():
    from sre_agent.tools import LiveBackend

    backend = LiveBackend(
        prometheus_url="http://localhost:9090",
        loki_url="http://localhost:3100",
        namespace="shop"
    )

    # Simulate Prometheus returning a 256 MiB memory limit.
    queries = []

    def fake_scalar(query):
        queries.append(query)

        if 'resource="memory"' in query:
            return 268435456  # 256 MiB in bytes

        return 2

    # Avoid making real Prometheus requests.
    backend._scalar = fake_scalar
    backend._trend = lambda query, minutes: "test data"

    result = backend.get_service_metrics("catalog")

    assert "configured memory limit: 256 MiB" in result
    assert any('resource="memory"' in q for q in queries)

    backend.http.close()


def test_live_backend_hpa_evidence():
    from types import SimpleNamespace
    from sre_agent.tools import LiveBackend

    backend = LiveBackend(
        prometheus_url="http://localhost:9090",
        loki_url="http://localhost:3100",
        namespace="shop",
    )

    def obj(**kwargs):
        return SimpleNamespace(**kwargs)

    # Simulate the Kubernetes HPA response.
    hpa = obj(
        spec=obj(
            max_replicas=3,
            metrics=[
                obj(
                    type="Resource",
                    resource=obj(
                        name="cpu",
                        target=obj(average_utilization=70),
                    ),
                )
            ],
        ),
        status=obj(
            current_replicas=3,
            desired_replicas=3,
            current_metrics=[
                obj(
                    type="Resource",
                    resource=obj(
                        name="cpu",
                        current=obj(average_utilization=62),
                    ),
                )
            ],
            conditions=[
                obj(type="ScalingActive", status="True"),
                obj(type="ScalingLimited", status="False"),
            ],
        ),
    )

    class FakeAutoscalingApi:
        def read_namespaced_horizontal_pod_autoscaler(
            self, name, namespace
        ):
            assert name == "checkout"
            assert namespace == "shop"
            return hpa

    class FakeKubernetesClient:
        def AutoscalingV2Api(self):
            return FakeAutoscalingApi()

    backend._kube = lambda: FakeKubernetesClient()

    try:
        evidence = backend.get_hpa_evidence("checkout")

        assert evidence["service"] == "checkout"
        assert evidence["current_replicas"] == 3
        assert evidence["desired_replicas"] == 3
        assert evidence["max_replicas"] == 3
        assert evidence["cpu_current_percent"] == 62
        assert evidence["cpu_target_percent"] == 70
        assert evidence["scaling_active"] == "True"
        assert evidence["scaling_limited"] == "False"
        collected_at = datetime.fromisoformat(evidence["collected_at"])
        assert collected_at.tzinfo is not None
        assert collected_at.utcoffset() is not None
    finally:
        backend.http.close()


def test_live_backend_hpa_missing_cpu_metrics():
    from types import SimpleNamespace
    from sre_agent.tools import LiveBackend

    def obj(**kwargs):
        return SimpleNamespace(**kwargs)

    backend = LiveBackend(
        prometheus_url="http://localhost:9090",
        loki_url="http://localhost:3100",
        namespace="shop",
    )

    hpa = obj(
        spec=obj(
            max_replicas=3,
            metrics=[
                obj(
                    type="Resource",
                    resource=obj(
                        name="cpu",
                        target=obj(average_utilization=70),
                    ),
                )
            ],
        ),
        status=obj(
            current_replicas=3,
            desired_replicas=3,
            current_metrics=[],
            conditions=[
                obj(type="ScalingActive", status="False"),
                obj(type="ScalingLimited", status="False"),
            ],
        ),
    )

    class FakeAutoscalingApi:
        def read_namespaced_horizontal_pod_autoscaler(
            self, name, namespace
        ):
            assert name == "checkout"
            assert namespace == "shop"
            return hpa

    class FakeKubernetesClient:
        def AutoscalingV2Api(self):
            return FakeAutoscalingApi()

    backend._kube = lambda: FakeKubernetesClient()

    try:
        evidence = backend.get_hpa_evidence("checkout")

        assert evidence["cpu_current_percent"] is None
        assert evidence["cpu_target_percent"] == 70
        assert evidence["scaling_active"] == "False"
    finally:
        backend.http.close()


def test_scale_rejected_without_capacity_evidence():
    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=5)
    )

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=None,
    )

    assert not result.allowed
    assert result.action.type == ActionType.ESCALATE
    assert any(
        "HPA evidence unavailable" in message
        for message in result.checks
    )


def test_scale_rejected_when_hpa_not_limited():
    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=5)
    )

    hpa_evidence = {
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

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=hpa_evidence,
    )

    assert not result.allowed
    assert result.action.type == ActionType.ESCALATE
    assert any(
        "HPA is not currently scaling-limited" in message
        for message in result.checks
    )


def test_scale_allowed_with_capacity_evidence():
    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=5)
    )

    hpa_evidence = {
        "service": "checkout",
        "namespace": "shop",
        "current_replicas": 3,
        "desired_replicas": 3,
        "max_replicas": 3,
        "cpu_current_percent": 95,
        "cpu_target_percent": 70,
        "scaling_active": "True",
        "scaling_limited": "True",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=hpa_evidence,
    )

    assert result.allowed
    assert result.action.type == ActionType.SCALE
    assert any(
        "HPA CPU 95% exceeds target 70%" in message
        for message in result.checks
    )


def test_scale_rejected_when_hpa_replicas_inconsistent():
    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=6)
    )

    evidence = {
        "service": "checkout",
        "namespace": "shop",
        "current_replicas": 2,
        "desired_replicas": 2,
        "max_replicas": 5,
        "cpu_current_percent": 95,
        "cpu_target_percent": 70,
        "scaling_active": "True",
        "scaling_limited": "True",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=evidence,
    )

    assert not result.allowed
    assert result.action.type == ActionType.ESCALATE
    assert any(
        "HPA replica counts do not confirm maximum capacity" in message
        for message in result.checks
    )

def test_scale_rejected_with_stale_hpa_evidence():
    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=5)
    )

    evidence = {
        "service": "checkout",
        "namespace": "shop",
        "current_replicas": 3,
        "desired_replicas": 3,
        "max_replicas": 3,
        "cpu_current_percent": 95,
        "cpu_target_percent": 70,
        "scaling_active": "True",
        "scaling_limited": "True",
        "collected_at": (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(),
    }

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=evidence,
    )

    assert not result.allowed
    assert result.action.type == ActionType.ESCALATE
    assert any(
        "HPA evidence is stale" in message
        for message in result.checks
    )


def test_scale_rejected_for_wrong_namespace():
    from datetime import datetime, timezone

    diagnosis = diag(
        ProposedAction(type=ActionType.SCALE, replicas=5)
    )

    evidence = {
        "service": "checkout",
        "namespace": "staging",
        "current_replicas": 3,
        "desired_replicas": 3,
        "max_replicas": 3,
        "cpu_current_percent": 95,
        "cpu_target_percent": 70,
        "scaling_active": "True",
        "scaling_limited": "True",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    result = check(
        diagnosis,
        CURRENT,
        KNOWN,
        hpa_evidence=evidence,
    )

    assert not result.allowed
    assert result.action.type == ActionType.ESCALATE

    assert any(
        "HPA evidence belongs to an unexpected namespace" in message
        for message in result.checks
    )
