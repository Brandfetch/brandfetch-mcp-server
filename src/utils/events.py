import json
import os
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

SOURCE = "mcp-server"

_sns = boto3.client(
    "sns",
    region_name=os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1",
)


def publish(event_name: str, urn: str, payload: dict) -> None:
    """Publish an analytics event to the SNS events topic"""
    topic_arn = os.environ.get("SNS_EVENTS_TOPIC_ARN")
    if not topic_arn:
        return

    try:
        message = json.dumps(payload, ensure_ascii=False)
        attributes = {
            "namespace": {
                "DataType": "String",
                "StringValue": event_name.split(".")[0],
            },
            "source": {
                "DataType": "String",
                "StringValue": SOURCE,
            },
            "urn": {
                "DataType": "String",
                "StringValue": urn,
            },
            "eventName": {
                "DataType": "String",
                "StringValue": event_name,
            },
            "type": {
                "DataType": "String",
                "StringValue": "event",
            },
        }

        _sns.publish(
            Message=message,
            MessageAttributes=attributes,
            MessageDeduplicationId=uuid4().hex,
            MessageGroupId=urn[-128:],
            TopicArn=topic_arn,
        )
    except ClientError as exc:
        print(f"[mcp-server] Failed to publish event {event_name}: {exc}")
    except Exception as exc:
        print(f"[mcp-server] Unexpected error publishing event {event_name}: {exc}")
