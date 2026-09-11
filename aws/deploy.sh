#!/usr/bin/env bash
# Deploys the apply-watcher Lambda + a 5-minute EventBridge schedule.
# Idempotent: re-run after editing src/index.ts or .env to update.
#
# Prereqs:
#   1. aws configure          (your AWS access key, e.g. region us-east-1)
#   2. cp .env.example .env   and fill in Photon creds + job URLs
#   3. bun                    (builds the Lambda bundle)
set -euo pipefail
cd "$(dirname "$0")"

FUNC=apply-watcher
ROLE=apply-watcher-role
RULE=apply-watcher-every-5min
STATE_PARAM=/apply-watcher/state

[ -f .env ] || { echo "Missing aws/.env — copy .env.example and fill it in."; exit 1; }
set -a; source .env; set +a
: "${PROJECT_ID:?}" "${PROJECT_SECRET:?}" "${RECIPIENTS:?}"
[ -n "${JOB_URLS:-}${TITLE_PATTERN:-}" ] || \
  { echo "Set TITLE_PATTERN and/or JOB_URLS in .env"; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region)
echo "Deploying to account $ACCOUNT, region $REGION"

# --- IAM role ---------------------------------------------------------------
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "Created role $ROLE (waiting for it to propagate...)"; sleep 10
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name ssm-state --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\",
    \"Action\": [\"ssm:GetParameter\", \"ssm:PutParameter\"],
    \"Resource\": \"arn:aws:ssm:$REGION:$ACCOUNT:parameter/apply-watcher/*\"},
   {\"Effect\": \"Allow\",
    \"Action\": \"ses:SendEmail\",
    \"Resource\": \"*\"}]}"

# --- Lambda -----------------------------------------------------------------
command -v bun >/dev/null || { echo "bun is required to build (https://bun.sh)"; exit 1; }
bun install --silent
# @aws-sdk/* ships with the nodejs runtime — keep it external, bundle the rest.
bun build src/index.ts --target node --external '@aws-sdk/*' --outfile dist/index.mjs >/dev/null
# The iMessage gRPC transport probes its optional peers with import.meta.resolve
# at runtime, so they must exist as node_modules entries in the zip even though
# their code is already bundled into index.mjs.
rm -rf dist/node_modules && mkdir -p dist/node_modules/@grpc
cp -R node_modules/nice-grpc node_modules/nice-grpc-common dist/node_modules/
cp -R node_modules/@grpc/grpc-js dist/node_modules/@grpc/
rm -f function.zip && (cd dist && zip -qr ../function.zip index.mjs node_modules)

ENV_JSON=$(python3 - <<'PY'
import json, os
print(json.dumps({"Variables": {k: os.environ[k] for k in
  ["PROJECT_ID","PROJECT_SECRET","RECIPIENTS"]}
  | {k: os.environ[k] for k in
     ["JOB_URLS","TITLE_PATTERN","AMAZON_COUNTRIES","ADMIN_KEY",
      "EMAIL_FROM","EMAIL_TO"]
     if os.environ.get(k)}
  | {"STATE_PARAM": os.environ.get("STATE_PARAM", "/apply-watcher/state")}}))
PY
)

if aws lambda get-function --function-name "$FUNC" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNC" \
    --zip-file fileb://function.zip >/dev/null
  aws lambda wait function-updated --function-name "$FUNC"
  aws lambda update-function-configuration --function-name "$FUNC" \
    --environment "$ENV_JSON" --timeout 120 >/dev/null
else
  aws lambda create-function --function-name "$FUNC" \
    --runtime nodejs22.x --handler index.handler \
    --role "arn:aws:iam::$ACCOUNT:role/$ROLE" \
    --zip-file fileb://function.zip \
    --environment "$ENV_JSON" --timeout 120 >/dev/null
fi
aws lambda wait function-updated --function-name "$FUNC"
# Cost guardrail: bounded concurrency, and logs expire.
# Not 1 — the same function serves both the EventBridge schedule and the admin
# page, so a watch run in flight would throttle an admin request, and API
# Gateway surfaces a throttled Lambda as a bare 503 "Service Unavailable".
# 5 keeps the runaway-cost ceiling while leaving room for the page.
aws lambda put-function-concurrency --function-name "$FUNC" \
  --reserved-concurrent-executions 5 >/dev/null
aws logs put-retention-policy --log-group-name "/aws/lambda/$FUNC" \
  --retention-in-days 14 2>/dev/null || true
echo "Lambda $FUNC deployed."

# --- Schedule ---------------------------------------------------------------
aws events put-rule --name "$RULE" --schedule-expression "rate(5 minutes)" >/dev/null
aws lambda add-permission --function-name "$FUNC" \
  --statement-id eventbridge-invoke --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:$REGION:$ACCOUNT:rule/$RULE" >/dev/null 2>&1 || true
aws events put-targets --rule "$RULE" --targets \
  "Id=1,Arn=arn:aws:lambda:$REGION:$ACCOUNT:function:$FUNC" >/dev/null
echo "Scheduled: every 5 minutes."

# --- Admin page (API Gateway HTTP API) ---------------------------------------
# Not a Lambda Function URL: newer accounts block public URL invokes at the
# account level and the only workaround grants unconditioned public
# lambda:InvokeFunction. API Gateway is public by design and sends the same
# payload-v2 event shape, so the handler doesn't care which fronts it.
if [ -n "${ADMIN_KEY:-}" ]; then
  API_NAME=apply-watcher-admin
  API_ID=$(aws apigatewayv2 get-apis \
    --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)
  if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
    API_ID=$(aws apigatewayv2 create-api --name "$API_NAME" \
      --protocol-type HTTP \
      --target "arn:aws:lambda:$REGION:$ACCOUNT:function:$FUNC" \
      --query ApiId --output text)
  fi
  aws lambda add-permission --function-name "$FUNC" \
    --statement-id apigw-invoke --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*" \
    >/dev/null 2>&1 || true
  echo "Admin page: https://$API_ID.execute-api.$REGION.amazonaws.com/?key=$ADMIN_KEY"
fi

# --- Smoke test -------------------------------------------------------------
echo "Invoking once now..."
aws lambda invoke --function-name "$FUNC" --payload '{}' /dev/stdout | cat
echo
echo "Done. Logs: aws logs tail /aws/lambda/$FUNC --follow"
