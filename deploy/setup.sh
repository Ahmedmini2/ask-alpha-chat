#!/usr/bin/env bash
# One-time AWS scaffolding for Ask Alpha on ECS Fargate (me-central-1).
# Creates: ECR repo, CloudWatch log group, ECS cluster, and the two IAM roles.
# It does NOT create the secret, ALB, ACM cert, or services — those need values only you
# have (real secret values, a domain, a cert). See README.md steps 1 and 5-7 for those.
#
# Prereqs: awscli v2 logged in (aws configure), Docker, and `envsubst` (gettext).
# Edit iam-task-role-policy.json first: replace YOUR_ASSETS_BUCKET with your real bucket.
set -euo pipefail
cd "$(dirname "$0")"

export AWS_REGION="${AWS_REGION:-me-central-1}"
export REGION="$AWS_REGION"
export ACCT="$(aws sts get-caller-identity --query Account --output text)"
echo "Account $ACCT  Region $REGION"

# --- ECR ---
aws ecr create-repository --repository-name askalpha --region "$REGION" >/dev/null 2>&1 \
  && echo "created ECR repo askalpha" || echo "ECR repo askalpha exists"

# --- CloudWatch log group ---
aws logs create-log-group --log-group-name /ecs/askalpha --region "$REGION" >/dev/null 2>&1 \
  && echo "created log group /ecs/askalpha" || echo "log group exists"

# --- ECS cluster ---
aws ecs create-cluster --cluster-name askalpha --region "$REGION" >/dev/null
echo "ensured ECS cluster askalpha"

# --- IAM: task execution role (pull image, read secret, write logs) ---
aws iam create-role --role-name askalpha-exec \
  --assume-role-policy-document file://iam-trust-ecs-tasks.json >/dev/null 2>&1 \
  && echo "created role askalpha-exec" || echo "role askalpha-exec exists"
aws iam attach-role-policy --role-name askalpha-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
sed "s/REGION/$REGION/g; s/ACCT/$ACCT/g" iam-exec-secrets-policy.json > /tmp/askalpha-exec-secrets.json
aws iam put-role-policy --role-name askalpha-exec --policy-name read-app-secret \
  --policy-document file:///tmp/askalpha-exec-secrets.json
echo "attached secrets-read to askalpha-exec"

# --- IAM: task role (app's own AWS calls: Bedrock + S3 assets) ---
aws iam create-role --role-name askalpha-task \
  --assume-role-policy-document file://iam-trust-ecs-tasks.json >/dev/null 2>&1 \
  && echo "created role askalpha-task" || echo "role askalpha-task exists"
aws iam put-role-policy --role-name askalpha-task --policy-name app-access \
  --policy-document file://iam-task-role-policy.json
echo "attached app-access to askalpha-task"

echo
echo "Done. Next: create the secret (README step 1), then ALB+cert (step 5) and"
echo "services (step 7). After services exist, run ./deploy.sh for every code push."
