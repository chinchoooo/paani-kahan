"""Alert delivery.

Production path (what the demo shows):
  SAM-built Lambda 'PollerFunction' (runs every 15 min on EventBridge; locally via
  `sam local invoke`) -> asks the app to re-run the forecast -> publishes each commute
  alert to the SNS topic 'paani-alerts' on LocalStack -> SNS fans out to the SQS queue
  'paani-alerts-app' -> this module long-polls SQS and streams alerts to browsers (SSE).

If LocalStack is not running, the UI's 'Run nowcast update' button delivers alerts
in-process instead, and the status panel says so.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque

LOCALSTACK_URL = os.getenv("LOCALSTACK_URL", "http://localhost:4566")
QUEUE_NAME = os.getenv("ALERT_QUEUE", "paani-alerts-app")
TOPIC_NAME = os.getenv("ALERT_TOPIC", "paani-alerts")
PIPELINE_LOG: deque = deque(maxlen=30)


class Bus:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self):
        q = asyncio.Queue(maxsize=50)
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def publish(self, event: dict):
        if not self.loop:
            return
        for q in list(self.subs):
            self.loop.call_soon_threadsafe(_put, q, event)


def _put(q, ev):
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        pass


def log(step, detail=""):
    PIPELINE_LOG.appendleft({"at": time.strftime("%H:%M:%S"), "step": step, "detail": detail})


class SqsListener(threading.Thread):
    daemon = True

    def __init__(self, bus: Bus):
        super().__init__()
        self.bus = bus
        self.connected = False
        self.queue_url = None
        self.error = None

    def run(self):
        import boto3
        sqs = boto3.client("sqs", endpoint_url=LOCALSTACK_URL, region_name="us-east-1",
                           aws_access_key_id="test", aws_secret_access_key="test")
        while True:
            try:
                if not self.queue_url:
                    # idempotent: SNS topic -> SQS queue (same as localstack/init-aws.sh)
                    sns = boto3.client("sns", endpoint_url=LOCALSTACK_URL, region_name="us-east-1",
                                       aws_access_key_id="test", aws_secret_access_key="test")
                    topic = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]
                    self.queue_url = sqs.create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
                    arn = sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
                    subs = sns.list_subscriptions_by_topic(TopicArn=topic).get("Subscriptions", [])
                    if not any(s["Endpoint"] == arn for s in subs):
                        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn,
                                      Attributes={"RawMessageDelivery": "true"})
                    self.connected, self.error = True, None
                    log("SQS connected", f"{QUEUE_NAME} on LocalStack")
                r = sqs.receive_message(QueueUrl=self.queue_url, WaitTimeSeconds=5, MaxNumberOfMessages=10)
                for m in r.get("Messages", []):
                    body = json.loads(m["Body"])
                    if "Message" in body and body.get("Type") == "Notification":
                        body = json.loads(body["Message"])  # SNS envelope
                    log("SQS message received", body.get("headline", ""))
                    self.bus.publish({"type": "alert", "via": "LocalStack SNS → SQS", **body})
                    sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])
            except Exception as e:  # noqa: BLE001
                self.connected, self.queue_url = False, None
                self.error = f"{type(e).__name__}"
                time.sleep(5)
