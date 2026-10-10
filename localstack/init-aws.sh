#!/bin/bash
# Runs inside LocalStack when it is ready: SNS topic -> SQS queue the app listens on.
set -e
TOPIC_ARN=$(awslocal sns create-topic --name paani-alerts --query TopicArn --output text)
QUEUE_URL=$(awslocal sqs create-queue --queue-name paani-alerts-app --query QueueUrl --output text)
QUEUE_ARN=$(awslocal sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text)
awslocal sns subscribe --topic-arn "$TOPIC_ARN" --protocol sqs --notification-endpoint "$QUEUE_ARN" \
  --attributes RawMessageDelivery=true
echo "paani: SNS $TOPIC_ARN -> SQS $QUEUE_ARN ready"
