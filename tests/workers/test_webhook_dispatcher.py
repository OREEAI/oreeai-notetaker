import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.workers import webhook_dispatcher as wd

SECRET = "shhhhhhhhhhhhhhhh"


def make_call(**overrides: object) -> Call:
    values: dict[str, object] = {
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "user_ref": "test-user-1",
        "consent_ack": True,
        "status": CallStatus.done,
        "end_reason": "call_ended",
        "webhook_url": "http://receiver.example/hooks/x",
        "webhook_secret": SECRET,
    }
    values.update(overrides)
    call = Call(
        meeting_url=str(values["meeting_url"]),
        user_ref=str(values["user_ref"]),
        consent_ack=bool(values["consent_ack"]),
        webhook_url=str(values["webhook_url"]),
        webhook_secret=str(values["webhook_secret"]),
    )
    call.status = values["status"]
    call.end_reason = values["end_reason"]
    return call


async def record_sleep(seconds: float, into: list[float]) -> None:
    into.append(seconds)
    await asyncio.sleep(0)


async def test_signature_matches_reference_scheme() -> None:
    body = b'{"x": 1}'
    timestamp = "1700000000"
    expected = hmac.new(
        SECRET.encode(), f"{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    assert wd.build_signature(SECRET, timestamp, body) == expected
    assert len(expected) == 64


async def test_deliver_success_sets_delivered_at(db_session: AsyncSession) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    sleeps: list[float] = []
    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: record_sleep(s, sleeps),
    )

    assert delivered is True
    assert call.webhook_delivered_at is not None
    assert call.webhook_attempts == 1
    assert sleeps == []

    request = seen[0]
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-OT-Timestamp"].isdigit()
    expected = hmac.new(
        SECRET.encode(),
        f"{request.headers['X-OT-Timestamp']}:".encode() + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-OT-Signature"] == expected


async def test_deliver_4xx_is_permanent_give_up(db_session: AsyncSession) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    sleeps: list[float] = []
    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        sleep=lambda s: record_sleep(s, sleeps),
    )

    assert delivered is False
    assert call.webhook_delivered_at is None
    assert call.webhook_attempts == 1
    assert sleeps == []


async def test_deliver_5xx_retries_full_backoff(db_session: AsyncSession) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    sleeps: list[float] = []
    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        sleep=lambda s: record_sleep(s, sleeps),
    )

    assert delivered is False
    assert call.webhook_delivered_at is None
    assert call.webhook_attempts == settings.webhook_max_attempts
    assert sleeps == [1.0, 4.0, 16.0, 64.0]


async def test_deliver_recovers_after_transient_5xx(db_session: AsyncSession) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    statuses = [500, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses.pop(0))

    sleeps: list[float] = []
    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: record_sleep(s, sleeps),
    )

    assert delivered is True
    assert call.webhook_attempts == 2
    assert sleeps == [1.0]


async def test_deliver_network_error_retries(db_session: AsyncSession) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    sleeps: list[float] = []
    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: record_sleep(s, sleeps),
    )

    assert delivered is False
    assert call.webhook_attempts == settings.webhook_max_attempts
    assert sleeps == [1.0, 4.0, 16.0, 64.0]


async def test_payload_shape_excludes_secret(db_session: AsyncSession) -> None:
    call = make_call(failure_reason=None)
    call.transcript = [{"speaker": "S0", "text": "hi"}]
    call.webhook_attempts = 0
    db_session.add(call)
    await db_session.flush()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await wd.deliver(call, db_session, transport=httpx.MockTransport(handler), sleep=asyncio.sleep)

    payload = json.loads(seen[0].content)
    assert set(payload) == {
        "call_id",
        "status",
        "end_reason",
        "failure_reason",
        "user_ref",
        "transcript",
        "audio_url",
        "created_at",
        "finished_at",
    }
    assert payload["status"] == "done"
    assert payload["end_reason"] == "call_ended"
    assert payload["failure_reason"] is None
    assert payload["user_ref"] == "test-user-1"
    assert payload["transcript"] == [{"speaker": "S0", "text": "hi"}]
    assert payload["audio_url"] is None
    assert SECRET not in seen[0].content.decode()


async def test_receiver_side_skew_rejection(
    db_session: AsyncSession,
) -> None:
    """Reference receiver: verifies our signature scheme and the 5-minute
    skew window the shared spec puts on the receiver."""
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    await wd.deliver(call, db_session, transport=httpx.MockTransport(handler), sleep=asyncio.sleep)
    request = received[0]

    def reference_receiver(timestamp: str, signature: str, body: bytes) -> str:
        expected = hmac.new(
            SECRET.encode(), f"{timestamp}:".encode() + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return "bad-signature"
        if abs(time.time() - int(timestamp)) > settings.webhook_timestamp_skew:
            return "stale-timestamp"
        return "accepted"

    assert (
        reference_receiver(
            request.headers["X-OT-Timestamp"], request.headers["X-OT-Signature"], request.content
        )
        == "accepted"
    )

    stale_ts = str(int(time.time()) - settings.webhook_timestamp_skew - 1)
    stale_sig = wd.build_signature(SECRET, stale_ts, request.content)
    assert reference_receiver(stale_ts, stale_ts, request.content) == "bad-signature"
    assert reference_receiver(stale_ts, stale_sig, request.content) == "stale-timestamp"


async def test_already_delivered_call_skips(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    call = make_call()
    call.webhook_delivered_at = datetime.now(UTC)
    call.webhook_attempts = 2
    db_session.add(call)
    await db_session.flush()

    delivered = await wd.deliver(
        call,
        db_session,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        sleep=asyncio.sleep,
    )

    assert delivered is True
    assert call.webhook_attempts == 2


async def test_no_secret_in_failure_logs(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    call = make_call()
    db_session.add(call)
    await db_session.flush()

    sleeps: list[float] = []

    with caplog.at_level(logging.DEBUG):
        await wd.deliver(
            call,
            db_session,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            sleep=lambda s: record_sleep(s, sleeps),
        )

    assert SECRET not in caplog.text
    await db_session.refresh(call)
    assert SECRET not in json.dumps(wd.build_payload(call))
