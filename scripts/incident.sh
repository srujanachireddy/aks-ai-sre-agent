#!/usr/bin/env bash
# Causes (and stops) incidents on purpose, and shows what the platform and the agent did about them.
#
#   ./scripts/incident.sh payments     payments goes down -> checkout errors     (agent should escalate: payments)
#   ./scripts/incident.sh memory       catalog runs out of memory, OOMKilled     (agent should raise the memory limit)
#   ./scripts/incident.sh cpu          checkout CPU saturated, latency climbs    (agent should scale out)
#   ./scripts/incident.sh bad-deploy   how to ship the buggy v1.4.0 release      (canary or agent rolls it back)
#   ./scripts/incident.sh status       pods, rollouts and firing alerts
#   ./scripts/incident.sh agent        what the agent concluded, and its pull requests
#   ./scripts/incident.sh stop         end every chaos experiment
set -euo pipefail
source "$(dirname "$0")/lib.sh"

connect_kubectl >/dev/null

case "${1:-}" in
  payments) kubectl apply -f "$ROOT/chaos/payments-outage.yaml"
            ok "payments is down for 15 minutes. Expect ShopErrorBudgetBurn on checkout in ~3-5 minutes." ;;
  memory)   kubectl apply -f "$ROOT/chaos/memory-leak.yaml"
            ok "catalog will be OOMKilled repeatedly. Expect ShopPodsRestarting in ~5-10 minutes." ;;
  cpu)      kubectl apply -f "$ROOT/chaos/cpu-saturation.yaml"
            ok "checkout CPU is saturated. Expect ShopLatencyHigh in ~5-7 minutes." ;;
  bad-deploy)
    cat <<EOF
A bad deploy arrives through Git, like a real release. On GitHub, edit
  gitops/apps/checkout/values.yaml
and change:
  image:
    tag: v1.4.0          # was v1.3.2

Commit to main. Then watch:  ./scripts/incident.sh status

Two outcomes to demo:
  1. canary.analysis: true (default) -> Argo Rollouts sends 1 pod of v1.4.0 traffic, sees its error
     rate above 5%, and aborts on its own. Production never fully gets the bad version.
  2. canary.analysis: false          -> the bad version rolls out fully, ShopErrorBudgetBurn fires,
     and the agent investigates and proposes rolling back to v1.3.2 as a pull request.
EOF
    ;;
  status)
    step "Pods"; kubectl -n shop get pods -o wide
    step "Rollouts"; kubectl -n shop get rollouts.argoproj.io
    step "Chaos experiments"; kubectl -n shop get stresschaos,podchaos 2>/dev/null || true
    step "Firing alerts"
    kubectl -n observability exec statefulset/alertmanager-monitoring-kube-prometheus-alertmanager -c alertmanager -- \
      wget -qO- 'http://localhost:9093/api/v2/alerts?active=true' |
      jq -r '.[] | "  \(.labels.alertname)  service=\(.labels.service // "-")  \(.annotations.summary // "")"' ;;
  agent)
    kubectl -n sre-agent exec deploy/sre-agent -- python -c '
import json, urllib.request
items = json.load(urllib.request.urlopen("http://localhost:8080/incidents"))
if not items: print("No investigations yet.")
for i, inc in items.items():
    d = inc.get("diagnosis") or {}
    print("=" * 70)
    print(inc["alert"]["name"], "on", inc["alert"]["service"], "-", inc["status"])
    if d:
        print("Category:   ", d["category"], "(confidence %.0f%%)" % (d["confidence"] * 100))
        print("Root cause: ", d["root_cause"])
        print("Final action:", inc.get("final_action"))
        for e in d.get("evidence", []): print("  evidence:", e)
        for c in inc.get("guardrails", []): print("  guardrail:", c)
        print("Pull request:", inc.get("pull_request") or "(dry run, not sent)")
        print("Tools used:", ", ".join(t["tool"] for t in inc.get("tool_calls", [])), "| tokens:", inc.get("tokens"))
' ;;
  stop) kubectl delete -f "$ROOT/chaos/" --ignore-not-found; ok "All chaos experiments stopped" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
