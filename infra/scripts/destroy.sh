#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/infra/terraform"
terraform plan -destroy -out=destroy.tfplan
cat <<'NOTICE'
This destroys Terraform-managed infrastructure. Review the plan for anything you need to preserve.
NOTICE
read -r -p "Destroy all resources in this Terraform state? Type DESTROY to continue: " answer
if [[ "$answer" != "DESTROY" ]]; then
  echo "Cancelled."
  rm -f destroy.tfplan
  exit 0
fi
terraform apply destroy.tfplan
rm -f destroy.tfplan
