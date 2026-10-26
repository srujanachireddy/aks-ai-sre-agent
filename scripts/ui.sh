#!/usr/bin/env bash
# Opens Argo CD, Grafana and the agent's API to ONE IP address: your laptop's.
#
#   ./scripts/ui.sh 203.0.113.25     (find your laptop's IP at https://api.ipify.org)
#   ./scripts/ui.sh --close          remove the public addresses again
#
# Each gets an Azure load balancer IP that only accepts traffic from that address.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

LAPTOP_IP="${1:-}"
[[ -n "$LAPTOP_IP" ]] || fail "Usage: $0 <your-laptop-public-ip>   (open https://api.ipify.org on your laptop to find it)"

connect_kubectl >/dev/null

if [[ "$LAPTOP_IP" == "--close" ]]; then
  kubectl -n argocd delete service argocd-public --ignore-not-found
  kubectl -n observability delete service grafana-public --ignore-not-found
  kubectl -n sre-agent delete service sre-agent-public --ignore-not-found
  ok "Public addresses removed"
  exit 0
fi
[[ "$LAPTOP_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "'$LAPTOP_IP' is not an IPv4 address."

expose() { # namespace name selector port targetPort
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $2
  namespace: $1
spec:
  type: LoadBalancer
  loadBalancerSourceRanges: ["$LAPTOP_IP/32"]
  selector: {$3}
  ports:
    - port: $4
      targetPort: $5
EOF
}
expose argocd argocd-public "app.kubernetes.io/name: argocd-server" 443 8080
expose observability grafana-public "app.kubernetes.io/name: grafana, app.kubernetes.io/instance: monitoring" 80 3000
expose sre-agent sre-agent-public "app.kubernetes.io/name: sre-agent" 80 8080

step "Waiting for public IPs"
ip_of() {
  local ip=""
  for _ in $(seq 1 30); do
    ip="$(kubectl -n "$1" get service "$2" -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
    [[ -n "$ip" ]] && break
    sleep 5
  done
  echo "$ip"
}
ARGO="$(ip_of argocd argocd-public)"
GRAFANA="$(ip_of observability grafana-public)"
AGENT="$(ip_of sre-agent sre-agent-public)"

ARGO_PW="$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
GRAFANA_PW="$(kubectl -n observability get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d)"

cat <<EOF

Open these on your laptop (only $LAPTOP_IP can reach them):

  Argo CD   https://$ARGO          user: admin   password: $ARGO_PW
            (self-signed certificate: click "Advanced" -> "Proceed")
  Grafana   http://$GRAFANA        user: admin   password: $GRAFANA_PW
            Dashboards -> "Shop SLOs and AI agent"
  Agent     http://$AGENT/incidents   (the agent's investigations, as JSON)

Close them later with: ./scripts/ui.sh --close
EOF
