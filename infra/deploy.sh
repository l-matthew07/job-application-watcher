#!/usr/bin/env bash
# Deploys the company-sentiment scraper stack.
# Idempotent: re-run after changing twitter_sentiment/ or the template.
#
# Prereqs:
#   1. aws configure          (credentials + region)
#   2. sam --version          (https://docs.aws.amazon.com/serverless-application-model/)
#   3. An X API bearer token with recent-search access (a paid tier).
#
# Usage:
#   X_BEARER_TOKEN=AAAA... ./deploy.sh          # first deploy, creates the secret
#   ./deploy.sh                                 # later deploys, reuses it
#
# Env knobs: STACK_NAME, SECRET_NAME, COMPANIES_FILE, SCHEDULE, ALARM_EMAIL
set -euo pipefail
cd "$(dirname "$0")"

STACK_NAME=${STACK_NAME:-twitter-sentiment}
SECRET_NAME=${SECRET_NAME:-$STACK_NAME/x-bearer-token}
COMPANIES_FILE=${COMPANIES_FILE:-../companies.yaml}
SCHEDULE=${SCHEDULE:-rate(30 minutes)}
ALARM_EMAIL=${ALARM_EMAIL:-}

command -v aws >/dev/null || { echo "aws CLI is required"; exit 1; }
command -v sam >/dev/null || { echo "sam CLI is required (AWS SAM)"; exit 1; }

REGION=$(aws configure get region || echo "${AWS_REGION:-}")
[ -n "$REGION" ] || { echo "No region configured — run 'aws configure'."; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying $STACK_NAME to account $ACCOUNT, region $REGION"

# --- Bearer token secret ----------------------------------------------------
# Kept in Secrets Manager, never a template parameter: a NoEcho parameter is
# redacted in the console but still travels through CloudFormation.
if SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
      --query ARN --output text 2>/dev/null); then
  if [ -n "${X_BEARER_TOKEN:-}" ]; then
    aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
      --secret-string "$X_BEARER_TOKEN" >/dev/null
    echo "Updated secret $SECRET_NAME"
  else
    echo "Reusing secret $SECRET_NAME"
  fi
else
  [ -n "${X_BEARER_TOKEN:-}" ] || {
    echo "Secret $SECRET_NAME does not exist yet."
    echo "Re-run with: X_BEARER_TOKEN=<your token> $0"
    exit 1
  }
  SECRET_ARN=$(aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "X API bearer token for $STACK_NAME" \
    --secret-string "$X_BEARER_TOKEN" --query ARN --output text)
  echo "Created secret $SECRET_NAME"
fi

# --- Stage the deployment package -------------------------------------------
# SAM builds whatever sits under CodeUri, so the package is assembled here
# rather than pointing CodeUri at the repo root and shipping the job-watcher
# code along with it.
[ -f "$COMPANIES_FILE" ] || {
  echo "No companies file at $COMPANIES_FILE"
  echo "Copy companies.example.yaml to companies.yaml and edit it."
  exit 1
}
rm -rf build && mkdir -p build
cp -R ../twitter_sentiment build/
cp requirements.txt build/
cp "$COMPANIES_FILE" build/twitter_sentiment/companies.yaml
find build -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "Staged build/ ($(du -sh build | cut -f1))"

# --- Validate, build, deploy -------------------------------------------------
sam validate --lint
sam build

PARAMS=(
  "BearerTokenSecretArn=$SECRET_ARN"
  "Schedule=$SCHEDULE"
)
[ -n "$ALARM_EMAIL" ] && PARAMS+=("AlarmEmail=$ALARM_EMAIL")

sam deploy \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${PARAMS[@]}"

FUNC=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text)

# --- Smoke test --------------------------------------------------------------
echo "Invoking once now..."
aws lambda invoke --function-name "$FUNC" --payload '{}' \
  --cli-binary-format raw-in-base64-out /dev/stdout | cat
echo
[ -n "$ALARM_EMAIL" ] && echo "Confirm the SNS subscription sent to $ALARM_EMAIL."
echo "Done. Logs: aws logs tail /aws/lambda/$FUNC --follow"
