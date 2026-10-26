"""Serves the same read-only investigation tools over MCP (Model Context Protocol).

Any MCP client can use them: Claude Desktop, VS Code / GitHub Copilot agent mode, or
your own agent. Every tool is marked read-only.

  sre-agent-mcp                         # stdio, for local clients
  sre-agent-mcp --http --port 8765      # streamable HTTP, for in-cluster clients

Environment: PROMETHEUS_URL, LOKI_URL, TARGET_NAMESPACE (default "shop").
"""

from __future__ import annotations

import argparse
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .tools import LiveBackend, ToolBackend, call_tool

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def build_server(backend: ToolBackend) -> MCPServer:
    server = MCPServer(
        name="aks-sre-tools",
        instructions="Read-only tools for investigating incidents in a Kubernetes namespace. "
        "Start with get_service_metrics, then correlate with deploys, events, logs and dependencies.",
    )

    @server.tool(annotations=READ_ONLY)
    def get_service_metrics(service: str, minutes: int = 30) -> str:
        """Golden signals for a service: request rate, error rate, p99 latency, CPU/memory vs limits, restarts, replicas."""
        return call_tool(backend, "get_service_metrics", {"service": service, "minutes": minutes})

    @server.tool(annotations=READ_ONLY)
    def get_recent_logs(service: str, minutes: int = 30) -> str:
        """Recent error-level log lines for a service, deduplicated with counts."""
        return call_tool(backend, "get_recent_logs", {"service": service, "minutes": minutes})

    @server.tool(annotations=READ_ONLY)
    def get_kubernetes_events(namespace: str) -> str:
        """Recent Kubernetes warning events in a namespace."""
        return call_tool(backend, "get_kubernetes_events", {"namespace": namespace})

    @server.tool(annotations=READ_ONLY)
    def get_deploy_history(service: str) -> str:
        """Recent revisions of a service with image tags and rollout times."""
        return call_tool(backend, "get_deploy_history", {"service": service})

    @server.tool(annotations=READ_ONLY)
    def get_dependencies(service: str) -> str:
        """Downstream dependencies of a service and their current health."""
        return call_tool(backend, "get_dependencies", {"service": service})

    return server


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    backend = LiveBackend(
        os.getenv("PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.observability:9090"),
        os.getenv("LOKI_URL", "http://loki-gateway.observability"),
        os.getenv("TARGET_NAMESPACE", "shop"),
    )
    server = build_server(backend)
    if args.http:
        server.run("streamable-http", host="0.0.0.0", port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
