#!/usr/bin/env bash
# Deploys the whole platform from Azure Cloud Shell (Bash). Safe to run again: it only applies differences.
#
#   ./scripts/up.sh
#
# Optional settings:
#   LOCATION=eastus ./scripts/up.sh                          region (default eastus2)
#   OPENAI_MODEL=gpt-4o-mini OPENAI_MODEL_VERSION=2024-07-18 ./scripts/up.sh
#   GITHUB_TOKEN=github_pat_... ./scripts/up.sh              lets the agent open real pull requests
set -euo pipefail
source "$(dirname "$0")/lib.sh"

LOCATION="${LOCATION:-eastus2}"
OPENAI_LOCATION="${OPENAI_LOCATION:-$LOCATION}"
OPENAI_MODEL="${OPENAI_MODEL:-gpt-4.1-mini}"
OPENAI_MODEL_VERSION="${OPENAI_MODEL_VERSION:-2025-04-14}"
NODE_SIZE="${NODE_SIZE:-Standard_B4s_v2}"
export LOCATION OPENAI_LOCATION OPENAI_MODEL OPENAI_MODEL_VERSION NODE_SIZE

# ---------------------------------------------------------------------------
step "1/9  Your Azure account"
SUB_NAME="$(az account show --query name -o tsv)"
MY_IP="$(curl -s https://api.ipify.org)"
ok "Subscription: $SUB_NAME"
ok "Cloud Shell public IP: $MY_IP (only this IP can reach the Kubernetes API)"
ok "Region: $LOCATION, model: $OPENAI_MODEL ($OPENAI_MODEL_VERSION)"
read -rp "    Deploy into '$SUB_NAME'? Type yes: " answer
[[ "$answer" == "yes" ]] || fail "Stopped. Nothing changed."

# ---------------------------------------------------------------------------
step "2/9  Checking the container images are built and public"
for ref in shop:v1.3.2 shop:v1.4.0 sre-agent:0.1.0; do
  name="${ref%%:*}"; tag="${ref##*:}"
  token="$(curl -s "https://ghcr.io/token?scope=repository:$REPO/$name:pull" | jq -r '.token // empty')"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/$REPO/$name/manifests/$tag")"
  if [[ "$code" == "200" ]]; then
    ok "ghcr.io/$REPO/$ref"
  else
    fail "Image ghcr.io/$REPO/$ref is not publicly pullable (HTTP $code).
    1. Check the 'build' workflow succeeded: https://github.com/$REPO/actions
    2. Make each package public: https://github.com/srujanachireddy?tab=packages
       -> open the package -> Package settings -> Change visibility -> Public"
  fi
done

# ---------------------------------------------------------------------------
step "3/9  Registering Azure resource providers (first time: 2-5 minutes)"
PROVIDERS=(Microsoft.ContainerService Microsoft.CognitiveServices Microsoft.ManagedIdentity
           Microsoft.Storage Microsoft.Network Microsoft.Compute)
for ns in "${PROVIDERS[@]}"; do az provider register --namespace "$ns" >/dev/null; done
for ns in "${PROVIDERS[@]}"; do
  until [[ "$(az provider show --namespace "$ns" --query registrationState -o tsv)" == "Registered" ]]; do sleep 10; done
  ok "$ns"
done

# ---------------------------------------------------------------------------
step "4/9  Checking the node size, vCPU quota and model availability"
# Is this VM size offered to THIS subscription in this region? (Some subscriptions are restricted.)
SKU="$(az vm list-skus -l "$LOCATION" --size "$NODE_SIZE" --resource-type virtualMachines \
  --query "[?name=='$NODE_SIZE'] | [0]" -o json 2>/dev/null)"
FAMILY="$(jq -r '.family // empty' <<<"${SKU:-null}")"
RESTRICTED="$(jq -r '(.restrictions // []) | length' <<<"${SKU:-null}")"
VCPUS="$(jq -r '[.capabilities[]? | select(.name == "vCPUs") | .value][0] // empty' <<<"${SKU:-null}")"
# Fallback: the vCPU count is the number in the size name (Standard_B4s_v2 -> 4).
[[ "$VCPUS" =~ ^[0-9]+$ ]] || VCPUS="$(sed -E 's/^Standard_[A-Za-z]+([0-9]+).*/\1/' <<<"$NODE_SIZE")"
[[ "$VCPUS" =~ ^[0-9]+$ && "$VCPUS" -gt 0 ]] || VCPUS=4
[[ -n "${FAMILY:-}" ]] || fail "VM size $NODE_SIZE does not exist in $LOCATION. Try NODE_SIZE=Standard_D4s_v5 ./scripts/up.sh"
[[ "${RESTRICTED:-0}" == "0" ]] || fail "VM size $NODE_SIZE is not allowed for your subscription in $LOCATION. Re-run with another size, e.g. NODE_SIZE=Standard_D4s_v5 ./scripts/up.sh"
ok "$NODE_SIZE is allowed ($VCPUS vCPUs, quota family $FAMILY)"

read -r LIMIT USED < <(az vm list-usage -l "$LOCATION" --query "[?name.value=='$FAMILY'] | [0].[limit, currentValue]" -o tsv) || true
AVAILABLE=$(( ${LIMIT:-0} - ${USED:-0} ))
MAX_NODES=$(( AVAILABLE / VCPUS ))
(( MAX_NODES > 3 )) && MAX_NODES=3
(( MAX_NODES >= 1 )) || fail "Only $AVAILABLE vCPUs free for $FAMILY in $LOCATION. Request at least $(( VCPUS * 2 )) (portal: Quotas -> Compute) or choose another NODE_SIZE."
(( MAX_NODES >= 2 )) || warn "Quota allows only 1 node. The platform may be short of CPU; request more quota for $FAMILY if pods stay Pending."
export MAX_NODES
ok "$AVAILABLE vCPUs free -> cluster can grow to $MAX_NODES node(s)"

found="$(az cognitiveservices model list -l "$OPENAI_LOCATION" \
  --query "length([?model.name=='$OPENAI_MODEL' && model.version=='$OPENAI_MODEL_VERSION'])" -o tsv 2>/dev/null || echo 0)"
if [[ "${found:-0}" == "0" ]]; then
  echo "    Chat models available in $OPENAI_LOCATION:"
  az cognitiveservices model list -l "$OPENAI_LOCATION" \
    --query "[?kind=='OpenAI' && contains(model.name, 'gpt')].{model:model.name, version:model.version}" -o table | head -25
  fail "$OPENAI_MODEL $OPENAI_MODEL_VERSION is not offered in $OPENAI_LOCATION. Re-run with OPENAI_MODEL=... OPENAI_MODEL_VERSION=... from the list above."
fi
ok "$OPENAI_MODEL $OPENAI_MODEL_VERSION is available in $OPENAI_LOCATION"

# ---------------------------------------------------------------------------
step "5/9  Terraform state storage"
SA="$(state_account)"
az group create -n "$STATE_RG" -l "$LOCATION" -o none
if ! az storage account show -n "$SA" -g "$STATE_RG" -o none 2>/dev/null; then
  az storage account create -n "$SA" -g "$STATE_RG" -l "$LOCATION" --sku Standard_LRS \
    --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false -o none
  az storage account blob-service-properties update --account-name "$SA" -g "$STATE_RG" --enable-versioning true -o none
fi
az role assignment create --assignee-object-id "$(az ad signed-in-user show --query id -o tsv)" \
  --assignee-principal-type User --role "Storage Blob Data Contributor" \
  --scope "$(az storage account show -n "$SA" -g "$STATE_RG" --query id -o tsv)" -o none 2>/dev/null || true
for attempt in $(seq 1 12); do
  az storage container create -n tfstate --account-name "$SA" --auth-mode login -o none 2>/dev/null && break
  (( attempt == 12 )) && fail "Could not create the state container."
  warn "waiting for storage permissions ($attempt/12)..."; sleep 30
done
ok "State in $SA/tfstate (versioned, Entra ID access only)"

# ---------------------------------------------------------------------------
step "6/9  Azure infrastructure with Terraform (AKS + Azure OpenAI, about 10-15 minutes)"
write_backend_config
write_tfvars
tf_init
echo "    Review the plan, then type yes."
terraform -chdir="$ROOT/infra" apply

# ---------------------------------------------------------------------------
step "7/9  Connecting kubectl (Entra ID sign-in)"
connect_kubectl
kubectl get nodes

# ---------------------------------------------------------------------------
step "8/9  Installing Argo CD, then handing everything else to it"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null
helm upgrade --install argocd argo/argo-cd --version 10.10.3 -n argocd --create-namespace \
  -f "$ROOT/gitops/bootstrap/argocd-values.yaml" --wait --timeout 10m
ok "Argo CD installed"

kubectl create namespace sre-agent --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "    Optional: a GitHub fine-grained token for $REPO with Contents + Pull requests: Read and write."
  read -rsp "    Paste it (or press Enter to skip; the agent then runs in dry-run mode): " GITHUB_TOKEN; echo
fi
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  kubectl -n sre-agent create secret generic sre-agent-github --from-literal=token="$GITHUB_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  ok "Agent can open pull requests"
else
  warn "No token: the agent will write pull requests but not send them (dry-run)"
fi

kubectl apply -f "$ROOT/gitops/bootstrap/root-app.yaml"
AGENT_CLIENT_ID="$(terraform -chdir="$ROOT/infra" output -raw agent_client_id)" \
TENANT_ID="$(terraform -chdir="$ROOT/infra" output -raw tenant_id)" \
OPENAI_ENDPOINT="$(terraform -chdir="$ROOT/infra" output -raw openai_endpoint)" \
OPENAI_DEPLOYMENT="$(terraform -chdir="$ROOT/infra" output -raw openai_deployment)" \
python3 - "$ROOT/gitops/bootstrap/sre-agent-app.yaml.tmpl" <<'PY' | kubectl apply -f -
import os, sys
text = open(sys.argv[1]).read()
for key in ("AGENT_CLIENT_ID", "TENANT_ID", "OPENAI_ENDPOINT", "OPENAI_DEPLOYMENT"):
    text = text.replace("${" + key + "}", os.environ[key])
print(text)
PY

# ---------------------------------------------------------------------------
step "9/9  Waiting for every application to be Synced and Healthy (up to 25 minutes)"
for i in $(seq 1 50); do
  status="$(kubectl -n argocd get applications -o json | jq -r '.items[] | "\(.metadata.name) \(.status.sync.status // "-") \(.status.health.status // "-")"' | sort)"
  total="$(wc -l <<<"$status")"
  ready="$(grep -c ' Synced Healthy$' <<<"$status" || true)"
  echo "    [$((i / 2)) min] $ready/$total ready"
  if (( total >= 12 && ready == total )); then break; fi
  if (( i % 6 == 0 )); then grep -v ' Synced Healthy$' <<<"$status" | sed 's/^/      waiting: /'; fi
  sleep 30
done
kubectl -n argocd get applications

cat <<EOF

$(printf '\033[1;32m')Platform is up.$(printf '\033[0m')
  Open the dashboards from your laptop:   ./scripts/ui.sh <your-laptop-public-ip>
  Cause an incident:                      ./scripts/incident.sh
  Stop paying when you're done:           ./scripts/down.sh
EOF
