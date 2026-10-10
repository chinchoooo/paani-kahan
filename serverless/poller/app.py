"""Nowcast poller Lambda.

1. Ask the forecast service to re-run with the latest nowcast (POST /api/internal/tick).
2. Publish every commute alert it returns to the 'paani-alerts' SNS topic.

Runs on a 15-minute EventBridge schedule when deployed. Locally:
    sam build && sam local invoke PollerFunction --docker-network paani-net --env-vars env.json
or without SAM:
    python serverless/poller/app.py
"""
import json
import os
import time
import urllib.request

import boto3

APP_URL = os.environ.get("APP_URL", "http://localhost:8080")
AWS_ENDPOINT = os.environ.get("AWS_ENDPOINT") or None
TOPIC_NAME = os.environ.get("ALERTS_TOPIC_NAME", "paani-alerts")


def _sns():
    kw = {"region_name": os.environ.get("AWS_REGION", "us-east-1")}
    if AWS_ENDPOINT:  # LocalStack
        kw.update(endpoint_url=AWS_ENDPOINT, aws_access_key_id="test", aws_secret_access_key="test")
    return boto3.client("sns", **kw)


def handler(event, context):
    t0 = time.time()
    req = urllib.request.Request(f"{APP_URL}/api/internal/tick", method="POST",
                                 data=json.dumps({"source": "sam-poller", "deliver": "return"}).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=80) as r:
        result = json.loads(r.read())
    sns = _sns()
    topic_arn = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]  # idempotent: returns the existing ARN
    published = []
    for alert in result.get("alerts", []):
        msg = sns.publish(TopicArn=topic_arn, Subject="Paani Kahan commute alert",
                          Message=json.dumps(alert),
                          MessageAttributes={"user": {"DataType": "String", "StringValue": alert["user"]}})
        published.append(msg["MessageId"])
        print(f"published alert for {alert['user']}: {alert['headline']}")
    out = {"run_id": result.get("run_id"), "forecast": result.get("label"),
           "alerts_published": len(published), "topic": topic_arn, "seconds": round(time.time() - t0, 1)}
    print(json.dumps(out))
    return out


if __name__ == "__main__":
    os.environ.setdefault("AWS_ENDPOINT", "http://localhost:4566")
    AWS_ENDPOINT = os.environ["AWS_ENDPOINT"]
    print(handler({}, None))
