#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/infra/terraform"
REGION="${AWS_REGION:-ap-south-1}"
TAG="${IMAGE_TAG:-track2}"
export AWS_REGION="$REGION"

command -v aws >/dev/null || { echo "AWS CLI v2 is required" >&2; exit 1; }
command -v terraform >/dev/null || { echo "Terraform is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
aws sts get-caller-identity >/dev/null || { echo "AWS credentials are not configured. Configure an IAM Identity Center profile or another short-lived credential source first." >&2; exit 1; }

cd "$TF_DIR"
terraform init
terraform validate

echo "Creating only the two ECR repositories first so images can be pushed before ECS starts."
terraform apply -target=aws_ecr_repository.api -target=aws_ecr_repository.mock

cd "$ROOT"
AWS_REGION="$REGION" IMAGE_TAG="$TAG" ./infra/scripts/build-and-push.sh

cd "$TF_DIR"
terraform plan -out=tfplan
cat <<'NOTICE'
Review the plan carefully. This stack creates billable resources, including an ALB and NAT Gateway.
Do not proceed unless you understand the planned resources and cost exposure.
NOTICE
read -r -p "Apply the reviewed full plan now? Type APPLY to continue: " answer
if [[ "$answer" != "APPLY" ]]; then
  echo "Stopped without applying the full plan. ECR repositories and pushed images remain and may incur small storage charges."
  exit 0
fi
terraform apply tfplan
rm -f tfplan
terraform output
