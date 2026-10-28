from sre_agent.guardrails import Policy, check
from sre_agent.models import ActionType, Category, Diagnosis, ProposedAction
from sre_agent.tools import FixtureBackend, call_tool, redact

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
    assert check(diag(ProposedAction(type=ActionType.SCALE, replicas=6)), CURRENT, KNOWN).allowed
    assert not check(diag(ProposedAction(type=ActionType.SCALE, replicas=50)), CURRENT, KNOWN).allowed
    assert not check(diag(ProposedAction(type=ActionType.SCALE, replicas=2)), CURRENT, KNOWN).allowed


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