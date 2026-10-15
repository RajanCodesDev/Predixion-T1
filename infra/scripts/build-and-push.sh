#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/infra/terraform"
REGION="${AWS_REGION:-ap-south-1}"
TAG="${IMAGE_TAG:-track2}"
PROJECT="${PROJECT_NAME:-collections-challenge}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
API_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}/api"
MOCK_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT}/mock"

# ECR repositories are created by Terraform first. This script is intended after bootstrap apply.
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
docker build -t "${API_REPO}:${TAG}" -f "$ROOT/Dockerfile" "$ROOT"
docker build -t "${MOCK_REPO}:${TAG}" -f "$ROOT/mock/Dockerfile" "$ROOT"
docker push "${API_REPO}:${TAG}"
docker push "${MOCK_REPO}:${TAG}"
printf '\nPushed images:\n  %s\n  %s\n' "${API_REPO}:${TAG}" "${MOCK_REPO}:${TAG}"
