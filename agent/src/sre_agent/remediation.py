"""Turns an approved action into a GitOps pull request. It never applies anything to the cluster.

The GitOps repo keeps each service's settings in gitops/apps/<service>/values.yaml:

    image:
      tag: v1.3.2
    replicas: 2
    resources:
      limits:
        memory: 256Mi

Argo CD deploys whatever is merged to main. So the agent's only power is to open a PR;
a human reviews and merges it, and Argo Rollouts canaries the change.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

import httpx
import yaml

from .guardrails import GuardrailResult
from .models import ActionType, Investigation


def values_path(service: str) -> str:
    return f"gitops/apps/{service}/values.yaml"


def current_settings(values: dict) -> dict:
    mem = str(values.get("resources", {}).get("limits", {}).get("memory", "0Mi"))
    match = re.match(r"^(\d+)(Mi|Gi)$", mem)
    mem_mi = int(match.group(1)) * (1024 if match and match.group(2) == "Gi" else 1) if match else 0
    autoscaling = values.get("autoscaling", {}) or {}
    # With an HPA, "capacity" means the ceiling the autoscaler may reach.
    replicas = autoscaling.get("maxReplicas", 1) if autoscaling.get("enabled") else values.get("replicas", 1)
    return {"image_tag": values.get("image", {}).get("tag"), "replicas": replicas, "memory_limit_mi": mem_mi}


def set_yaml_value(text: str, path: list[str], value: str) -> str:
    """Change one scalar in block-style YAML, keeping every comment and all other lines as they are.

    A reviewer then sees a one-line diff instead of a reformatted file.
    Raises KeyError if the path does not exist.
    """
    lines = text.splitlines(keepends=True)
    depth, parent_indent = 0, -1
    for i, line in enumerate(lines):
        stripped = line.lstrip(" ")
        if not stripped.strip() or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent <= parent_indent:  # left the block we were searching in
            break
        m = re.match(r"^([A-Za-z0-9_.\-]+):(\s*)(.*?)(\s+#.*)?$", stripped.rstrip("\n"))
        if not m or m.group(1) != path[depth]:
            continue
        if depth == len(path) - 1:
            comment = m.group(4) or ""
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{' ' * indent}{path[depth]}: {value}{comment}{newline}"
            return "".join(lines)
        depth, parent_indent = depth + 1, indent
    raise KeyError(".".join(path))


def apply_action(values_text: str, result: GuardrailResult) -> str:
    """Returns the new values.yaml text. Only the one field the action owns is changed."""
    values = yaml.safe_load(values_text) or {}
    a = result.action
    if a.type == ActionType.ROLLBACK:
        target, new = ["image", "tag"], str(a.target_image_tag)
    elif a.type == ActionType.SCALE:
        if (values.get("autoscaling") or {}).get("enabled"):
            target, new = ["autoscaling", "maxReplicas"], str(a.replicas)
        else:
            target, new = ["replicas"], str(a.replicas)
    elif a.type == ActionType.INCREASE_MEMORY:
        target, new = ["resources", "limits", "memory"], f"{a.memory_limit_mi}Mi"
    else:
        return values_text
    try:
        return set_yaml_value(values_text, target, new)
    except KeyError:
        # Field not present yet: fall back to a full rewrite (comments are lost, the change is still correct).
        node = values
        for key in target[:-1]:
            node = node.setdefault(key, {})
        node[target[-1]] = yaml.safe_load(new)
        return yaml.safe_dump(values, sort_keys=False)


def pr_body(inv: Investigation, result: GuardrailResult) -> str:
    d = inv.diagnosis
    assert d is not None
    evidence = "\n".join(f"- {e}" for e in d.evidence) or "- (none)"
    checks = "\n".join(f"- {c}" for c in result.checks)
    tools = ", ".join(f"`{t.tool}`" for t in inv.tool_calls) or "none"
    return f"""## Incident: {inv.alert.name} on `{d.service}`

**Root cause ({d.category.value}, confidence {d.confidence:.0%}):** {d.root_cause}

### Evidence
{evidence}

### Proposed change
`{result.action.type.value}`: {result.action.rationale}

### Guardrail checks
{checks}

### How this was produced
Investigated by the AI SRE agent in {inv.steps} steps using read-only tools ({tools}).
Tokens: {inv.prompt_tokens} in / {inv.completion_tokens} out. Duration: {inv.duration_seconds}s.

> The agent cannot merge. Review the evidence, then merge to let Argo CD roll this out as a canary.
"""


@dataclass
class GitHubRepo:
    """Minimal GitHub REST client: branch, commit one file, open PR."""

    owner: str
    repo: str
    token: str
    base: str = "main"
    api: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> "GitHubRepo | None":
        slug, token = os.getenv("GITOPS_REPO"), os.getenv("GITHUB_TOKEN")
        if not slug or not token:
            return None
        owner, repo = slug.split("/", 1)
        return cls(owner, repo, token, base=os.getenv("GITOPS_BASE_BRANCH", "main"))

    @staticmethod
    def read_public(slug: str, path: str, branch: str = "main") -> str:
        """Read a file from a public repo without a token (used in dry-run mode)."""
        r = httpx.get(f"https://raw.githubusercontent.com/{slug}/{branch}/{path}", timeout=10)
        r.raise_for_status()
        return r.text

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=f"{self.api}/repos/{self.owner}/{self.repo}", timeout=20,
                            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})

    def read_file(self, path: str) -> tuple[str, str]:
        with self._client() as c:
            r = c.get(f"/contents/{path}", params={"ref": self.base})
            r.raise_for_status()
            data = r.json()
            return base64.b64decode(data["content"]).decode(), data["sha"]

    def open_pr(self, branch: str, path: str, new_text: str, file_sha: str, title: str, body: str) -> str:
        with self._client() as c:
            base_sha = c.get(f"/git/ref/heads/{self.base}").raise_for_status().json()["object"]["sha"]
            c.post("/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}).raise_for_status()
            c.put(f"/contents/{path}", json={
                "message": title, "branch": branch, "sha": file_sha,
                "content": base64.b64encode(new_text.encode()).decode(),
            }).raise_for_status()
            pr = c.post("/pulls", json={"title": title, "head": branch, "base": self.base, "body": body}).raise_for_status()
            return pr.json()["html_url"]
