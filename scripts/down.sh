#!/usr/bin/env bash
# Deletes everything that costs money (AKS, Azure OpenAI, identities).
#
#   ./scripts/down.sh          keep the small state storage account (cents/month) so up.sh can rebuild
#   ./scripts/down.sh --all    also delete the state storage (project finished)
set -euo pipefail
source "$(dirname "$0")/lib.sh"

step "Destroying the platform"
write_backend_config
write_tfvars
tf_init
echo "    Review what will be deleted, then type yes."
terraform -chdir="$ROOT/infra" destroy

if [[ "${1:-}" == "--all" ]]; then
  step "Deleting Terraform state storage"
  az group delete -n "$STATE_RG" --yes
fi

step "Resource groups left for this project"
az group list --query "[?starts_with(name, 'rg-sre')].name" -o tsv
