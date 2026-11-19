#!/usr/bin/env bash
# =====================================================================
# FastSense · Bootstrap de LocalStack (SQS + S3)
# Se ejecuta automáticamente en /etc/localstack/init/ready.d/
# =====================================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
QUEUE="fastsense-interactions-queue"
DLQ="fastsense-interactions-dlq"
BUCKET="${S3_BUCKET:-fastsense-datalake-models}"

echo "[init-aws] region=$REGION queue=$QUEUE dlq=$DLQ bucket=$BUCKET"

# ---------------------------- SQS -----------------------------------
awslocal sqs create-queue --queue-name "$DLQ" --region "$REGION" >/dev/null 2>&1 || true

DLQ_URL=$(awslocal sqs get-queue-url --queue-name "$DLQ" --region "$REGION" --query 'QueueUrl' --output text)
DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url "$DLQ_URL" \
  --attribute-names QueueArn \
  --region "$REGION" \
  --query 'Attributes.QueueArn' \
  --output text)

QUEUE_URL=$(awslocal sqs create-queue \
  --queue-name "$QUEUE" \
  --region "$REGION" \
  --query 'QueueUrl' \
  --output text)

# El RedrivePolicy debe ser un string JSON dentro del mapa de atributos.
ATTRS=$(python3 - "$DLQ_ARN" <<'PY'
import json
import sys

policy = json.dumps({"deadLetterTargetArn": sys.argv[1], "maxReceiveCount": "3"})
print(json.dumps({
    "RedrivePolicy": policy,
    "VisibilityTimeout": "60",
    "MessageRetentionPeriod": "345600",
}))
PY
)

awslocal sqs set-queue-attributes \
  --queue-url "$QUEUE_URL" \
  --attributes "$ATTRS" \
  --region "$REGION"

# ---------------------------- S3 ------------------------------------
awslocal s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1 || true
awslocal s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled \
  --region "$REGION"

echo "[init-aws] colas:"
awslocal sqs list-queues --region "$REGION"
echo "[init-aws] versionado S3:"
awslocal s3api get-bucket-versioning --bucket "$BUCKET" --region "$REGION"
