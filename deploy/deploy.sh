#!/usr/bin/env bash
# Build the image, push to ECR, register fresh task-def revisions, and roll all three
# services. Run this for EVERY deploy (after setup.sh + the services already exist).
#
# Required env:  CORS_ORIGINS=https://your-web-domain   (comma-separated for several)
# Optional env:  AWS_REGION (default me-central-1), BEDROCK_MODEL_ID
set -euo pipefail
cd "$(dirname "$0")"

export AWS_REGION="${AWS_REGION:-me-central-1}"
export REGION="$AWS_REGION"
export ACCT="$(aws sts get-caller-identity --query Account --output text)"
export ECR="$ACCT.dkr.ecr.$REGION.amazonaws.com/askalpha"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
: "${CORS_ORIGINS:?Set CORS_ORIGINS to your web origin, e.g. https://app.allegiance.ae}"
export SECRET_ARN="$(aws secretsmanager describe-secret --secret-id askalpha/prod \
  --region "$REGION" --query ARN --output text)"

echo "==> build + push  $ECR:latest"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"
docker build --platform linux/amd64 -t "$ECR:latest" ..   # build context = repo root
docker push "$ECR:latest"

echo "==> register task definitions"
for svc in api worker bot; do
  envsubst < "task-def-$svc.json" > "/tmp/td-$svc.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/td-$svc.json" \
    --region "$REGION" >/dev/null
  echo "   registered askalpha-$svc"
done

echo "==> roll services"
for svc in api worker bot; do
  if aws ecs update-service --cluster askalpha --service "askalpha-$svc" \
       --task-definition "askalpha-$svc" --force-new-deployment --region "$REGION" >/dev/null 2>&1; then
    echo "   deploying askalpha-$svc"
  else
    echo "   askalpha-$svc not created yet — create it per README step 6 (task def is registered)"
  fi
done
echo "Done. Watch: aws ecs describe-services --cluster askalpha --services askalpha-api --region $REGION"
