"""Signed webhook delivery for terminal call transitions.

Implements the shared webhook contract: HMAC-SHA256 over ``{ts}:{body}``
(hex) in ``X-OT-Signature``, unix-seconds string in ``X-OT-Timestamp``,
at-least-once delivery with 5 attempts at 1/4/16/64/256 s backoff and a
10 s per-attempt timeout. 2xx = delivered; 4xx = permanent give-up (the
receiver answered, retrying cannot help); 5xx and network errors retry.

``webhook_secret`` never appears in the payload, in logs, or anywhere
outside the HMAC computation. The dispatcher never raises into the
runner: unexpected errors are logged and left for the next terminal
event or a manual replay.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.models.call import Call

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]

DEFAULT_BACKOFF_S: tuple[float, ...] = (1.0, 4.0, 16.0, 64.0, 256.0)


def build_signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest over ``{timestamp}:{body}``."""
    message = f"{timestamp}:".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_payload(call: Call) -> dict[str, Any]:
    return {
        "call_id": str(call.id),
        "status": call.status.value if call.status is not None else None,
        "end_reason": call.end_reason,
        "failure_reason": call.failure_reason,
        "user_ref": call.user_ref,
        "transcript": call.transcript,
        "audio_url": call.audio_url,
        "created_at": call.created_at.isoformat() if call.created_at else None,
        "finished_at": call.updated_at.isoformat() if call.updated_at else None,
    }


async def deliver(
    call: Call,
    session: AsyncSession,
    *,
    sleep: Sleeper = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Deliver the terminal-status webhook for ``call``.

    Increments ``webhook_attempts`` for every attempt (flushed as it
    goes) and sets ``webhook_delivered_at`` on success. The caller owns
    the commit. ``transport`` exists for tests (httpx.MockTransport).
    Returns True when delivered, False when permanently given up on;
    never raises.
    """
    if call.webhook_delivered_at is not None:
        return True

    body = json.dumps(build_payload(call), default=str).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-OT-Timestamp": timestamp,
        "X-OT-Signature": build_signature(call.webhook_secret, timestamp, body),
    }
    url = call.webhook_url
    max_attempts = settings.webhook_max_attempts
    backoff = DEFAULT_BACKOFF_S

    client_kwargs: dict[str, Any] = {"timeout": settings.webhook_http_timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        for attempt in range(max_attempts):
            call.webhook_attempts += 1
            await session.flush()
            try:
                response = await client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    "webhook network error for call %s (attempt %s/%s): %s",
                    call.id,
                    attempt + 1,
                    max_attempts,
                    exc,
                )
            else:
                if 200 <= response.status_code < 300:
                    call.webhook_delivered_at = datetime.now(UTC)
                    await session.flush()
                    logger.info(
                        "webhook delivered for call %s after %s attempt(s)",
                        call.id,
                        attempt + 1,
                    )
                    return True
                if response.status_code < 500:
                    logger.error(
                        "webhook receiver permanently rejected call %s with HTTP %s "
                        "after %s attempt(s) — not retrying",
                        call.id,
                        response.status_code,
                        attempt + 1,
                    )
                    return False
                logger.warning(
                    "webhook receiver error for call %s (attempt %s/%s): HTTP %s",
                    call.id,
                    attempt + 1,
                    max_attempts,
                    response.status_code,
                )
            if attempt < max_attempts - 1:
                await sleep(backoff[min(attempt, len(backoff) - 1)])

    logger.error(
        "webhook delivery gave up for call %s after %s attempts — webhook_delivered_at stays null",
        call.id,
        max_attempts,
    )
    return False
