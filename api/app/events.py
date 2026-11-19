"""Publicación asíncrona de interacciones en SQS (boto3)."""

from __future__ import annotations

import time
from typing import Any

from app.aws import publish_interaction
from app.cache import cache
from app.logging_utils import get_logger

logger = get_logger("fastsense.events")


def publish_with_retry(message: dict[str, Any], attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        if publish_interaction(message):
            cache.incr("metrics:tracking_accepted")
            return True
        logger.warning(
            "sqs_publish_retry event_id=%s attempt=%d", message.get("event_id"), attempt
        )
        time.sleep(0.2 * attempt)
    logger.error("sqs_publish_exhausted event_id=%s", message.get("event_id"))
    cache.incr("metrics:tracking_failed")
    return False
