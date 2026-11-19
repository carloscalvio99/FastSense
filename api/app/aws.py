"""Clientes boto3 desacoplados: LocalStack en local, AWS real en producción."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Optional

import boto3
from botocore.config import Config

from app.config import settings
from app.logging_utils import get_logger

logger = get_logger("fastsense.aws")

_BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=2,
    read_timeout=5,
)


def _kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
        "config": _BOTO_CONFIG,
    }
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


@lru_cache(maxsize=1)
def s3_client():
    return boto3.client("s3", **_kwargs())


@lru_cache(maxsize=1)
def sqs_client():
    return boto3.client("sqs", **_kwargs())


@lru_cache(maxsize=1)
def queue_url() -> Optional[str]:
    if settings.sqs_queue_url:
        return settings.sqs_queue_url
    try:
        return sqs_client().get_queue_url(QueueName=settings.sqs_queue_name)["QueueUrl"]
    except Exception as exc:
        logger.warning("sqs_queue_url_unavailable error=%s", type(exc).__name__)
        return None


def get_queue_depth(url: Optional[str] = None) -> int:
    target = url or queue_url()
    if not target:
        return -1
    try:
        attributes = sqs_client().get_queue_attributes(
            QueueUrl=target,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        ).get("Attributes", {})
        return int(attributes.get("ApproximateNumberOfMessages", 0)) + int(
            attributes.get("ApproximateNumberOfMessagesNotVisible", 0)
        )
    except Exception:
        return -1


def publish_interaction(message: dict[str, Any]) -> bool:
    target = queue_url()
    if not target:
        logger.error("sqs_publish_failed reason=queue_url_missing event_id=%s", message.get("event_id"))
        return False
    try:
        sqs_client().send_message(
            QueueUrl=target,
            MessageBody=json.dumps(message, ensure_ascii=False, default=str),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": str(message.get("event_type", "unknown"))},
                "event_id": {"DataType": "String", "StringValue": str(message.get("event_id", "unknown"))},
            },
        )
        return True
    except Exception as exc:
        logger.error(
            "sqs_publish_failed event_id=%s error=%s",
            message.get("event_id"),
            type(exc).__name__,
        )
        return False
