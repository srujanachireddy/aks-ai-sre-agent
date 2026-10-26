"""Alertmanager webhook receiver.

Alertmanager POSTs firing alerts to /alertmanager. For each new alert the agent investigates
in the background, guardrails check the proposal, and (if a GitOps repo is configured) a pull
request is opened. Results are kept in memory and exposed at /incidents for the demo UI and
for screenshots.

Environment:
  PROMETHEUS_URL, LOKI_URL, TARGET_NAMESPACE     tool backend
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT  model (or OPENAI_* vars)
  GITOPS_REPO=owner/repo, GITHUB_TOKEN            open PRs (otherwise dry-run: the PR is rendered, not sent)
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .agent import investigate
from .guardrails import check
from .llm import ChatModel
from .models import ActionType, Alert, Investigation
from .remediation import GitHubRepo, apply_action, current_settings, pr_body, values_path
from .tools import LiveBackend, ToolBackend

log = logging.getLogger("sre_agent")

INVESTIGATIONS = Counter("sre_agent_investigations_total", "Investigations by outcome", ["category", "action", "pr"])
DURATION = Histogram("sre_agent_investigation_seconds", "Time to diagnosis", buckets=(5, 10, 20, 30, 60, 120, 300))
TOKENS = Counter("sre_agent_llm_tokens_total", "LLM tokens used", ["kind"])

DEDUP_SECONDS = 15 * 60  # don't re-investigate the same alert fingerprint within 15 minutes


class IncidentStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, float] = {}

    def should_start(self, fingerprint: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._seen.get(fingerprint, 0) < DEDUP_SECONDS:
                return False
            self._seen[fingerprint] = now
            return True

    def put(self, incident_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.items[incident_id] = data


def create_app(backend: ToolBackend, model_factory, repo: GitHubRepo | None) -> FastAPI:
    app = FastAPI(title="AI SRE agent")
    store = IncidentStore()
    app.state.store = store

    def handle(incident_id: str, alert: Alert) -> None:
        store.put(incident_id, {"status": "investigating", "alert": alert.model_dump()})
        model: ChatModel = model_factory()
        inv: Investigation = investigate(alert, backend, model)
        TOKENS.labels("prompt").inc(inv.prompt_tokens)
        TOKENS.labels("completion").inc(inv.completion_tokens)
        DURATION.observe(inv.duration_seconds)
        d = inv.diagnosis
        assert d is not None

        pr_url, rendered, guard_checks, final_action = None, None, [], d.action.type.value
        try:
            if repo:
                text, sha = repo.read_file(values_path(d.service))
            elif os.getenv("GITOPS_REPO"):
                # Dry-run: no token, but the repo is public, so guardrails still see the real current values.
                text, sha = GitHubRepo.read_public(os.environ["GITOPS_REPO"], values_path(d.service)), ""
            else:
                text, sha = "", ""
            current = current_settings(_safe_yaml(text))
            known = _known_tags(backend, d.service)
            guard = check(d, current, known)
            guard_checks, final_action = guard.checks, guard.action.type.value
            body = pr_body(inv, guard)
            if guard.allowed and guard.action.type != ActionType.ESCALATE:
                title = f"sre-agent: {guard.action.type.value} {d.service} ({inv.alert.name})"
                if repo:
                    branch = f"sre-agent/{d.service}-{incident_id[:8]}"
                    pr_url = repo.open_pr(branch, values_path(d.service), apply_action(text, guard), sha, title, body)
                else:
                    rendered = {"title": title, "body": body}
            else:
                rendered = {"title": f"Escalation: {inv.alert.name} on {d.service}", "body": body}
        except Exception as exc:
            log.exception("remediation step failed")
            inv.error = f"remediation failed: {exc}"

        INVESTIGATIONS.labels(d.category.value, final_action, "yes" if pr_url else "no").inc()
        store.put(incident_id, {
            "status": "done", "alert": alert.model_dump(), "diagnosis": d.model_dump(mode="json"),
            "final_action": final_action, "guardrails": guard_checks, "pull_request": pr_url,
            "dry_run_pr": rendered, "tool_calls": [t.model_dump() for t in inv.tool_calls],
            "steps": inv.steps, "tokens": inv.prompt_tokens + inv.completion_tokens,
            "duration_seconds": inv.duration_seconds, "error": inv.error,
        })

    @app.post("/alertmanager")
    def alertmanager(payload: dict[str, Any], tasks: BackgroundTasks) -> dict[str, Any]:
        started = []
        for a in payload.get("alerts", []):
            if a.get("status") != "firing":
                continue
            labels = a.get("labels", {})
            service = labels.get("service") or labels.get("app") or labels.get("deployment")
            if not service:
                continue
            fingerprint = a.get("fingerprint") or f"{labels.get('alertname')}:{service}"
            if not store.should_start(fingerprint):
                continue
            alert = Alert(
                name=labels.get("alertname", "unknown"), service=service,
                namespace=labels.get("namespace", "shop"), severity=labels.get("severity", "warning"),
                summary=a.get("annotations", {}).get("summary", ""), starts_at=a.get("startsAt", ""),
                fingerprint=fingerprint, labels=labels,
            )
            incident_id = str(uuid.uuid4())
            tasks.add_task(handle, incident_id, alert)
            started.append(incident_id)
        return {"accepted": started}

    @app.get("/incidents")
    def incidents() -> dict[str, Any]:
        return store.items

    @app.get("/incidents/{incident_id}")
    def incident(incident_id: str) -> dict[str, Any]:
        if incident_id not in store.items:
            raise HTTPException(404, "unknown incident")
        return store.items[incident_id]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _safe_yaml(text: str) -> dict:
    import yaml

    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


def _known_tags(backend: ToolBackend, service: str) -> set[str]:
    import re

    try:
        return set(re.findall(r"tag ([A-Za-z0-9_.\-]+)", backend.get_deploy_history(service)))
    except Exception:
        return set()


def main() -> None:
    import uvicorn

    from .llm import OpenAIChatModel

    logging.basicConfig(level=logging.INFO)
    backend = LiveBackend(
        os.getenv("PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.observability:9090"),
        os.getenv("LOKI_URL", "http://loki-gateway.observability"),
        os.getenv("TARGET_NAMESPACE", "shop"),
    )
    app = create_app(backend, OpenAIChatModel, GitHubRepo.from_env())
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
