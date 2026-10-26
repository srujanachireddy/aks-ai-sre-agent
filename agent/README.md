# AI SRE agent

An AI SRE agent. When an alert fires, it investigates with **read-only tools**, writes a
root-cause analysis, and proposes the smallest safe fix **as a GitOps pull request**. It
cannot change the cluster, and it cannot merge.

```
Alertmanager ──webhook──► agent ──read-only tools──► Prometheus · Loki · Kubernetes API
                            │
                            ├─ LLM (Azure OpenAI) picks tools, then submits a structured diagnosis
                            ├─ guardrails (plain code) accept it or downgrade it to "escalate"
                            └─ GitHub PR with the evidence ──► human merges ──► Argo CD canary
```

## Design choices

| Choice | Why |
|---|---|
| High-level tools (`get_service_metrics`, `get_deploy_history`...) instead of raw PromQL or kubectl | Smaller prompts, nothing destructive is reachable, and behaviour can be replayed from recorded incidents |
| Same tools served over **MCP** | Claude Desktop, VS Code Copilot or any MCP client can investigate with them too |
| Structured `submit_diagnosis` with a fixed action list | The model's output is validated data, not free text that has to be parsed |
| Guardrails in code, after the model | The model is treated as untrusted. Unknown tags, low confidence, unmanaged services and oversized changes all become "escalate" |
| Fix = pull request | Review, audit trail and rollback come from Git. Argo Rollouts canaries the merged change |
| Secret redaction on every tool result | Credentials found in logs never reach the model |
| Evaluated against a rule-based baseline | Shows where AI adds value, and where simple rules are enough |

## Run the tests and evals locally

```bash
cd agent
pip install -e ".[dev]"
pytest                                    # 23 tests: loop, guardrails, redaction, webhook, MCP
sre-agent-evals evals/scenarios            # rule-based baseline (no API key needed)

# with a model (Azure OpenAI)
export AZURE_OPENAI_ENDPOINT=https://<name>.openai.azure.com AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini AZURE_OPENAI_API_KEY=...
sre-agent-evals evals/scenarios --agent --runs 3
```

## Evaluation results

| System | Correct | Notes |
|---|---|---|
| Rule-based baseline | **2/5 (40%)** | Measured. Handles obvious cases, can't pick safe values, and blames the coincidental deploy in `misleading_deploy` |
| LLM agent | *not yet measured* | Run `sre-agent-evals --agent --runs 3` once Azure OpenAI is set up, and record the real numbers here |

## Scenarios

| Scenario | What happened | Correct answer |
|---|---|---|
| `bad_deploy` | v1.4.0 throws `KeyError` right after rollout | rollback to v1.3.2 |
| `memory_exhaustion` | cache grows until OOMKilled, no recent deploy | increase memory |
| `capacity` | traffic 4x, CPU pinned, HPA at max | scale out |
| `dependency_failure` | payments is crash-looping | escalate (payments) |
| `misleading_deploy` | errors began 14 minutes *before* an unrelated deploy | escalate (payments), **not** rollback |
