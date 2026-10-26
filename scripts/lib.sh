#!/usr/bin/env bash
# Shared helpers for the Cloud Shell scripts. Sourced, not run directly.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC2034 # used by the scripts that source this file
REPO="srujanachireddy/aks-ai-sre-agent"
STATE_RG="rg-sre-tfstate"
export PATH="$HOME/bin:$PATH"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Terraform state lives in a storage account whose name is derived from your subscription ID,
# so every script (and every new Cloud Shell session) finds the same one.
state_account() {
  local sub
  sub="$(az account show --query id -o tsv)"
  echo "stsre$(printf '%s' "$sub" | sha1sum | cut -c1-12)"
}

write_backend_config() {
  cat > "$ROOT/infra/backend.hcl" <<EOF
resource_group_name  = "$STATE_RG"
storage_account_name = "$(state_account)"
container_name       = "tfstate"
key                  = "lab/platform.tfstate"
use_azuread_auth     = true
EOF
}

write_tfvars() {
  cat > "$ROOT/infra/terraform.tfvars" <<EOF
subscription_id      = "$(az account show --query id -o tsv)"
admin_object_id      = "$(az ad signed-in-user show --query id -o tsv)"
operator_ip          = "$(curl -s https://api.ipify.org)"
location             = "${LOCATION:-eastus2}"
openai_location      = "${OPENAI_LOCATION:-${LOCATION:-eastus2}}"
openai_model         = "${OPENAI_MODEL:-gpt-4.1-mini}"
openai_model_version = "${OPENAI_MODEL_VERSION:-2025-04-14}"
max_nodes            = ${MAX_NODES:-2}
EOF
}

# terraform init can fail for a few minutes while a new storage role assignment propagates.
tf_init() {
  local attempt
  for attempt in $(seq 1 12); do
    if terraform -chdir="$ROOT/infra" init -input=false -reconfigure -backend-config=backend.hcl >/tmp/tf-init.log 2>&1; then
      return 0
    fi
    warn "waiting for storage permissions to take effect ($attempt/12)..."
    sleep 30
  done
  cat /tmp/tf-init.log
  fail "terraform init kept failing."
}

ensure_kubelogin() {
  if ! command -v kubelogin >/dev/null; then
    mkdir -p "$HOME/bin"
    az aks install-cli --install-location "$HOME/bin/kubectl" --kubelogin-install-location "$HOME/bin/kubelogin" >/dev/null
  fi
}

connect_kubectl() {
  local rg aks
  rg="$(terraform -chdir="$ROOT/infra" output -raw resource_group)"
  aks="$(terraform -chdir="$ROOT/infra" output -raw aks_name)"
  ensure_kubelogin
  az aks get-credentials -g "$rg" -n "$aks" --overwrite-existing >/dev/null
  kubelogin convert-kubeconfig -l azurecli
}
