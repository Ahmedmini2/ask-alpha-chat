# Deploying Ask Alpha to AWS ECS Fargate (me-central-1, UAE)

Moves production off Railway to **Fargate in `me-central-1`** so the app sits close to UAE
users. Three services share one image:

| Service | Tasks | Runs | Load balancer |
|---|---|---|---|
| `askalpha-api` | 2 → 10 (autoscaled) | `uvicorn app.main:app`, poller OFF | ✅ ALB / HTTPS |
| `askalpha-worker` | **1** (never more) | HeyGen poller only | ❌ |
| `askalpha-bot` | **1** (never more) | Telegram bot | ❌ |

> ⚠️ **Latency note:** the Supabase DB is still in Mumbai (`ap-south-1`). App-in-UAE → DB
> is ~35 ms × (many queries/turn). For true "as fast as possible," also move the Supabase
> project to `me-central-1` later and change only `DB_HOST` in the secret. This deploy is
> still a big win vs. Railway in the US/EU.

## Prerequisites
- AWS CLI v2, logged in (`aws configure`) with admin-ish rights.
- Docker, and `envsubst` (from the `gettext` package).
- A domain you can add a DNS record to (e.g. `api.askalpha.allegiance.ae`).
- Decide your web origin for CORS (e.g. `https://app.allegiance.ae`).

Set these in your shell for every step below:
```bash
export AWS_REGION=me-central-1
export CORS_ORIGINS=https://aredxb-next.vercel.app/          # comma-separate if several
cd deploy
```

---

## Step 1 — Edit the task-role bucket, then run the scaffolding
Open `iam-task-role-policy.json` and replace `YOUR_ASSETS_BUCKET` with your real S3 assets
bucket name (the eu-west-2 brochure/video bucket). Then:
```bash
chmod +x setup.sh deploy.sh
./setup.sh        # ECR repo, /ecs/askalpha log group, ECS cluster, both IAM roles
```

## Step 2 — Create the secret (real values)
```bash
aws secretsmanager create-secret --name askalpha/prod --region $AWS_REGION \
  --secret-string '{
    "DB_HOST":"aws-1-ap-south-1.pooler.supabase.com","DB_PORT":"5432",
    "DB_USER":"postgres.pqzsdxcjyqjjvfsunzak","DB_PASSWORD":"REPLACE","DB_NAME":"postgres",
    "SUPABASE_URL":"https://pqzsdxcjyqjjvfsunzak.supabase.co",
    "HEYGEN_API_KEY":"REPLACE","TELEGRAM_BOT_TOKEN":"REPLACE","DESCRIPT_API_TOKEN":"REPLACE",
    "FAL_KEY":"REPLACE","AYRSHARE_API_KEY":"REPLACE","PM_API_KEY":"REPLACE","PM_COMPANY_KEY":"REPLACE",
    "GOOGLE_AI_STUDIO_API_KEY":"REPLACE"
  }'
```
> Setting `SUPABASE_URL` turns ON the JWT auth from the earlier fix — make sure the web app
> forwards `Authorization: Bearer <token>` before you cut traffic over.

## Step 3 — Build, push, and register the task definitions
```bash
./deploy.sh
```
First run builds + pushes the image and registers all three task defs. The "roll services"
lines will say *not created yet* — expected; you create the services in Step 6.

## Step 4 — TLS certificate
Request a cert in **me-central-1** for your API domain and validate it via DNS:
```bash
aws acm request-certificate --domain-name api.askalpha.allegiance.ae \
  --validation-method DNS --region $AWS_REGION
```
Get the `CNAME` record from the cert and add it at your DNS provider; wait until status is
`ISSUED`. Save the cert ARN:
```bash
export CERT_ARN=arn:aws:acm:me-central-1:ACCT:certificate/xxxxxxxx
```

## Step 5 — Load balancer (uses your default VPC's public subnets)
```bash
export ACCT=$(aws sts get-caller-identity --query Account --output text)
export VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text --region $AWS_REGION)
export SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
  --query 'Subnets[].SubnetId' --output text --region $AWS_REGION)

# Security groups: ALB open on 443; tasks reachable only from the ALB on 8000.
export ALB_SG=$(aws ec2 create-security-group --group-name askalpha-alb --description "alb" \
  --vpc-id $VPC --query GroupId --output text --region $AWS_REGION)
export TASK_SG=$(aws ec2 create-security-group --group-name askalpha-task --description "tasks" \
  --vpc-id $VPC --query GroupId --output text --region $AWS_REGION)
aws ec2 authorize-security-group-ingress --group-id $ALB_SG --protocol tcp --port 443 \
  --cidr 0.0.0.0/0 --region $AWS_REGION
aws ec2 authorize-security-group-ingress --group-id $TASK_SG --protocol tcp --port 8000 \
  --source-group $ALB_SG --region $AWS_REGION

# ALB + target group (ip type) + HTTPS listener. Health check = /health (exists in main.py).
export ALB_ARN=$(aws elbv2 create-load-balancer --name askalpha-alb --type application \
  --subnets $SUBNETS --security-groups $ALB_SG \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text --region $AWS_REGION)
export TG_ARN=$(aws elbv2 create-target-group --name askalpha-api --protocol HTTP --port 8000 \
  --vpc-id $VPC --target-type ip --health-check-path /health \
  --query 'TargetGroups[0].TargetGroupArn' --output text --region $AWS_REGION)
aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTPS --port 443 \
  --certificates CertificateArn=$CERT_ARN \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN --region $AWS_REGION
export ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN \
  --query 'LoadBalancers[0].DNSName' --output text --region $AWS_REGION)
echo "ALB at: $ALB_DNS"
```

## Step 6 — Create the three services
```bash
NET="awsvpcConfiguration={subnets=[$(echo $SUBNETS | tr ' ' ',')],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}"

# API — behind the ALB, 2 tasks
aws ecs create-service --cluster askalpha --service-name askalpha-api \
  --task-definition askalpha-api --desired-count 2 --launch-type FARGATE \
  --network-configuration "$NET" \
  --load-balancers "targetGroupArn=$TG_ARN,containerName=api,containerPort=8000" \
  --health-check-grace-period-seconds 60 --region $AWS_REGION

# Worker — exactly 1, no LB
aws ecs create-service --cluster askalpha --service-name askalpha-worker \
  --task-definition askalpha-worker --desired-count 1 --launch-type FARGATE \
  --network-configuration "$NET" --region $AWS_REGION

# Bot — exactly 1, no LB
aws ecs create-service --cluster askalpha --service-name askalpha-bot \
  --task-definition askalpha-bot --desired-count 1 --launch-type FARGATE \
  --network-configuration "$NET" --region $AWS_REGION
```

## Step 7 — Autoscale the API (your "multiple users" lever)
```bash
aws application-autoscaling register-scalable-target --service-namespace ecs \
  --resource-id service/askalpha/askalpha-api --scalable-dimension ecs:service:DesiredCount \
  --min-capacity 2 --max-capacity 10 --region $AWS_REGION
aws application-autoscaling put-scaling-policy --service-namespace ecs \
  --resource-id service/askalpha/askalpha-api --scalable-dimension ecs:service:DesiredCount \
  --policy-name cpu60 --policy-type TargetTrackingScaling --region $AWS_REGION \
  --target-tracking-scaling-policy-configuration \
  '{"TargetValue":60.0,"PredefinedMetricSpecification":{"PredefinedMetricType":"ECSServiceAverageCPUUtilization"}}'
```

## Step 8 — DNS + verify
Point `api.askalpha.allegiance.ae` at the ALB (`$ALB_DNS`) via a Route 53 alias or a CNAME.
Then:
```bash
curl https://api.askalpha.allegiance.ae/health      # -> {"status":"ok"} (or similar)
```
Check logs in CloudWatch group `/ecs/askalpha` (streams `api/`, `worker/`, `bot/`). Confirm
the worker logs "HeyGen poller worker starting" and the api logs "RUN_HEYGEN_POLLER=false".

## Step 9 — Cut over & decommission
1. Point the web app's API base URL at the new domain.
2. Smoke-test: a chat turn, then a promo video (verify it gets captioned by the worker).
3. **Stop the Railway services** (keep them ~24h as instant rollback).

---

## Every future deploy
```bash
cd deploy && export AWS_REGION=me-central-1 CORS_ORIGINS=https://YOUR-WEB-DOMAIN
./deploy.sh
```

## Scaling / DB notes
- Each process uses up to `pool_size(5)+max_overflow(10)=15` DB connections
  (`app/db/session.py`). 10 API tasks + worker + bot ≈ 180 — keep under your Supabase pooler
  limit; lower `max_overflow` if needed.
- Never set `askalpha-worker` or `askalpha-bot` above **1** task (singleton poller; single
  Telegram long-poll consumer).
- After migrating, optionally switch `BEDROCK_MODEL_ID` to the APAC Claude inference profile
  so the LLM hop is regional too.
