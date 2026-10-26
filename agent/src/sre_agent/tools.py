"""Read-only investigation tools.

The agent never gets raw PromQL, shell or kubectl. It gets a small set of high-level,
read-only questions it can ask about a service. That keeps prompts small, makes the
agent's behaviour testable against recorded incidents, and means a confused model
cannot change anything.

Two backends implement the same interface:
- LiveBackend: Prometheus, Loki and the Kubernetes API inside the cluster.
- FixtureBackend: recorded answers from an incident scenario (used by tests and evals).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

MAX_RESULT_CHARS = 4000  # tool output budget per call, protects the context window

# Things that look like credentials are removed before any text reaches the model.
# Order matters: specific token formats first, then generic "key: value" pairs.
_SECRET_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/\-]+=*"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(AccountKey|SharedAccessKey|sig)=[^;&\s]+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)(\s*[:=]\s*)(?!\[REDACTED\])\S+"),
     r"\1\2[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_RULES:
        text = pattern.sub(replacement, text)
    return text


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


# JSON-schema tool definitions, shared by the LLM loop and the MCP server.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_service_metrics",
        "description": "Golden signals for a service over the last N minutes: request rate, error rate, "
        "p99 latency, CPU and memory vs limits, restarts, and current vs max replicas. "
        "Start here for almost every alert.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "minutes": {"type": "integer", "minimum": 5, "maximum": 180, "default": 30},
            },
            "required": ["service"],
        },
    },
    {
        "name": "get_recent_logs",
        "description": "Recent error-level log lines for a service (deduplicated, newest first).",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "minutes": {"type": "integer", "minimum": 5, "maximum": 120, "default": 30},
            },
            "required": ["service"],
        },
    },
    {
        "name": "get_kubernetes_events",
        "description": "Recent Kubernetes warning events in a namespace (OOMKilled, BackOff, FailedScheduling...).",
        "parameters": {
            "type": "object",
            "properties": {"namespace": {"type": "string"}},
            "required": ["namespace"],
        },
    },
    {
        "name": "get_deploy_history",
        "description": "Recent revisions of a service: revision number, image tag and when it was rolled out.",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
    {
        "name": "get_dependencies",
        "description": "Downstream services this service calls, with their current health (ready pods, error rate).",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
]

TOOL_NAMES = {spec["name"] for spec in TOOL_SPECS}


class ToolBackend(Protocol):
    def get_service_metrics(self, service: str, minutes: int = 30) -> str: ...
    def get_recent_logs(self, service: str, minutes: int = 30) -> str: ...
    def get_kubernetes_events(self, namespace: str) -> str: ...
    def get_deploy_history(self, service: str) -> str: ...
    def get_dependencies(self, service: str) -> str: ...


def call_tool(backend: ToolBackend, name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call safely. Unknown tools and bad arguments come back as text, never exceptions."""
    if name not in TOOL_NAMES:
        return f"ERROR: unknown tool '{name}'. Available: {sorted(TOOL_NAMES)}"
    try:
        result = getattr(backend, name)(**arguments)
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}"
    except Exception as exc:  # a broken data source must not crash the investigation
        return f"ERROR: {name} failed: {type(exc).__name__}: {exc}"
    return clip(redact(result))


# --------------------------------------------------------------------------- fixtures


class FixtureBackend:
    """Serves recorded tool answers from an incident scenario."""

    def __init__(self, responses: dict[str, Any]):
        self._responses = responses

    def _get(self, tool: str, key: str) -> str:
        value = self._responses.get(tool, {})
        if isinstance(value, dict):
            return str(value.get(key, value.get("*", f"No data for {key}.")))
        return str(value)

    def get_service_metrics(self, service: str, minutes: int = 30) -> str:
        return self._get("get_service_metrics", service)

    def get_recent_logs(self, service: str, minutes: int = 30) -> str:
        return self._get("get_recent_logs", service)

    def get_kubernetes_events(self, namespace: str) -> str:
        return self._get("get_kubernetes_events", namespace)

    def get_deploy_history(self, service: str) -> str:
        return self._get("get_deploy_history", service)

    def get_dependencies(self, service: str) -> str:
        return self._get("get_dependencies", service)


# --------------------------------------------------------------------------- live cluster


class LiveBackend:
    """Answers questions from Prometheus, Loki and the Kubernetes API.

    Metric names match the demo services (prometheus_client defaults) plus cAdvisor and
    kube-state-metrics, which kube-prometheus-stack installs.
    """

    def __init__(self, prometheus_url: str, loki_url: str, namespace: str = "shop", timeout: float = 10.0):
        self.prom = prometheus_url.rstrip("/")
        self.loki = loki_url.rstrip("/")
        self.namespace = namespace
        self.http = httpx.Client(timeout=timeout)
        self._k8s = None

    # -- helpers
    def _scalar(self, promql: str) -> float | None:
        resp = self.http.get(f"{self.prom}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else None

    def _trend(self, promql: str, minutes: int) -> str:
        """First, middle and last value over the window: enough to see 'when did it change'."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        resp = self.http.get(
            f"{self.prom}/api/v1/query_range",
            params={"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": max(60, minutes * 6)},
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return "n/a"
        values = result[0]["values"]
        picks = [values[0], values[len(values) // 2], values[-1]]
        return " -> ".join(
            f"{datetime.fromtimestamp(float(t), timezone.utc):%H:%M} {float(v):.3g}" for t, v in picks
        )

    def _kube(self):
        if self._k8s is None:
            from kubernetes import client, config  # optional dependency

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._k8s = client
        return self._k8s

    def _ready_pods_query(self, service: str) -> str:
        # Pod-level readiness works for Argo Rollouts as well as Deployments.
        return f'sum(kube_pod_status_ready{{namespace="{self.namespace}", pod=~"{service}-.*", condition="true"}})'

    # -- tools
    def get_service_metrics(self, service: str, minutes: int = 30) -> str:
        ns, w = self.namespace, "5m"
        sel = f'service="{service}"'
        pod = f'namespace="{ns}", pod=~"{service}-.*", container!=""'
        lines = [f"Service {service}, last {minutes} minutes (values: start -> middle -> now)"]
        queries = {
            "request rate (req/s)": f"sum(rate(app_requests_total{{{sel}}}[{w}]))",
            "error rate (5xx share)": f'sum(rate(app_requests_total{{{sel},status=~"5.."}}[{w}])) / sum(rate(app_requests_total{{{sel}}}[{w}]))',
            "p99 latency (s)": f"histogram_quantile(0.99, sum by (le) (rate(app_request_duration_seconds_bucket{{{sel}}}[{w}])))",
            "CPU used / limit": f"sum(rate(container_cpu_usage_seconds_total{{{pod}}}[{w}])) / sum(kube_pod_container_resource_limits{{namespace=\"{ns}\", pod=~\"{service}-.*\", resource=\"cpu\"}})",
            "memory used / limit": f"max(container_memory_working_set_bytes{{{pod}}} / on(pod, container) group_left kube_pod_container_resource_limits{{namespace=\"{ns}\", resource=\"memory\"}})",
            "container restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{ns}", pod=~"{service}-.*"}})',
        }
        for label, q in queries.items():
            lines.append(f"- {label}: {self._trend(q, minutes)}")
        ready = self._scalar(self._ready_pods_query(service))
        hpa_max = self._scalar(f'max(kube_horizontalpodautoscaler_spec_max_replicas{{namespace="{ns}", horizontalpodautoscaler="{service}"}})')
        lines.append(f"- replicas available: {ready if ready is not None else 'n/a'}, HPA max: {hpa_max if hpa_max is not None else 'no HPA'}")
        return "\n".join(lines)

    def get_recent_logs(self, service: str, minutes: int = 30) -> str:
        end = datetime.now(timezone.utc)
        query = f'{{namespace="{self.namespace}", app="{service}"}} |~ "(?i)(error|exception|traceback|timeout|refused|fatal|oom)"'
        resp = self.http.get(
            f"{self.loki}/loki/api/v1/query_range",
            params={"query": query, "start": int((end - timedelta(minutes=minutes)).timestamp() * 1e9),
                    "end": int(end.timestamp() * 1e9), "limit": 200, "direction": "backward"},
        )
        resp.raise_for_status()
        seen: dict[str, int] = {}
        for stream in resp.json()["data"]["result"]:
            for _, line in stream["values"]:
                key = re.sub(r"\d{2}:\d{2}:\d{2}[.\d]*|\b[0-9a-f]{8,}\b", "*", line)[:300]
                seen[key] = seen.get(key, 0) + 1
        if not seen:
            return f"No error-level logs for {service} in the last {minutes} minutes."
        top = sorted(seen.items(), key=lambda kv: -kv[1])[:20]
        return "\n".join(f"[x{count}] {line}" for line, count in top)

    def get_kubernetes_events(self, namespace: str) -> str:
        v1 = self._kube().CoreV1Api()
        events = v1.list_namespaced_event(namespace, field_selector="type=Warning").items
        events.sort(key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if not events:
            return f"No warning events in {namespace}."
        return "\n".join(
            f"{(e.last_timestamp or e.event_time):%H:%M} {e.involved_object.kind}/{e.involved_object.name} "
            f"{e.reason} (x{e.count or 1}): {e.message}"
            for e in events[:25]
        )

    def get_deploy_history(self, service: str) -> str:
        apps = self._kube().AppsV1Api()
        rs_list = apps.list_namespaced_replica_set(self.namespace, label_selector=f"app.kubernetes.io/name={service}").items
        rows = []
        for rs in rs_list:
            ann = rs.metadata.annotations or {}
            rev = ann.get("rollout.argoproj.io/revision") or ann.get("deployment.kubernetes.io/revision") or "?"
            image = rs.spec.template.spec.containers[0].image
            rows.append((int(rev) if rev.isdigit() else 0, rev, image.split(":")[-1], rs.metadata.creation_timestamp, rs.status.ready_replicas or 0))
        rows.sort(key=lambda r: r[0], reverse=True)
        if not rows:
            return f"No revisions found for {service}."
        now = datetime.now(timezone.utc)
        return "\n".join(
            f"revision {r[1]}: tag {r[2]}, rolled out {r[3]:%Y-%m-%d %H:%M} UTC "
            f"({int((now - r[3]).total_seconds() // 60)} min ago), ready pods {r[4]}"
            for r in rows[:6]
        )

    def get_dependencies(self, service: str) -> str:
        v1 = self._kube().CoreV1Api()
        svc = v1.read_namespaced_service(service, self.namespace)
        deps = (svc.metadata.annotations or {}).get("sre-agent.io/dependencies", "")
        if not deps:
            return f"{service} declares no dependencies (Service annotation sre-agent.io/dependencies)."
        lines = []
        for d in [x.strip() for x in deps.split(",") if x.strip()]:
            ready = self._scalar(self._ready_pods_query(d))
            err = self._scalar(f'sum(rate(app_requests_total{{service="{d}",status=~"5.."}}[5m])) / sum(rate(app_requests_total{{service="{d}"}}[5m]))')
            lines.append(f"- {d}: ready pods {ready if ready is not None else 'n/a'}, error rate {f'{err:.1%}' if err is not None else 'n/a'}")
        return "\n".join(lines)
