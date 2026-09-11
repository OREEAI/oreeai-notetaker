import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.core.exceptions import ConflictError, NotFoundError
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.repositories.call import CallRepository
from oreeai_notetaker.schemas.call import CallCreate
from oreeai_notetaker.services.call import CallService

VALID_MEETING_URL = "https://meet.google.com/abc-defg-hij"


def make_payload(**overrides: object) -> CallCreate:
    values: dict[str, object] = {
        "meeting_url": VALID_MEETING_URL,
        "user_ref": "test-user-1",
        "consent_ack": True,
        "webhook_url": "https://example.com/hooks/oreeai",
        "webhook_secret": "shhhhhhhhhhhhhhhh",
    }
    values.update(overrides)
    return CallCreate.model_validate(values)


@pytest.fixture
def service(db_session: AsyncSession, fake_cache: CacheService) -> CallService:
    return CallService(CallRepository(db_session), fake_cache)


async def test_create_returns_queued_call(service: CallService) -> None:
    call = await service.create(make_payload())
    assert call.status == CallStatus.queued
    assert call.user_ref == "test-user-1"
    assert call.transcript is None
    assert call.webhook_attempts == 0


async def test_create_rejects_at_ceiling(
    service: CallService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "call_concurrency_limit", 2)
    await service.create(make_payload())
    await service.create(make_payload())

    with pytest.raises(ConflictError, match="CALL_CONCURRENCY_LIMIT"):
        await service.create(make_payload())


async def test_terminal_calls_do_not_count_toward_ceiling(
    service: CallService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "call_concurrency_limit", 1)
    first = await service.create(make_payload())
    await service.mark_failed(first.id, "never_admitted")

    second = await service.create(make_payload())
    assert second.status == CallStatus.queued


async def test_get_missing_raises_not_found(service: CallService) -> None:
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


async def test_full_happy_path_transitions(service: CallService) -> None:
    call = await service.create(make_payload())

    joining = await service.mark_bot_started(call.id, "oreeai-bot-test")
    assert joining.status == CallStatus.joining
    assert joining.bot_container_name == "oreeai-bot-test"

    recording = await service.mark_recording(call.id)
    assert recording.status == CallStatus.recording

    processing = await service.mark_processing(call.id)
    assert processing.status == CallStatus.processing

    done = await service.mark_done(call.id, "call_ended")
    assert done.status == CallStatus.done
    assert done.end_reason == "call_ended"
    assert done.failure_reason is None


async def test_mark_done_writes_transcript_placeholder(
    service: CallService,
) -> None:
    call = await service.create(make_payload())
    await service.mark_bot_started(call.id, "oreeai-bot-test")
    await service.mark_recording(call.id)
    await service.mark_processing(call.id)
    done = await service.mark_done(call.id, "call_ended", transcript=[])
    assert done.status == CallStatus.done
    assert done.transcript == []


async def test_removed_recording_flows_through_processing(service: CallService) -> None:
    call = await service.create(make_payload())
    await service.mark_bot_started(call.id, "oreeai-bot-test")
    await service.mark_recording(call.id)
    processing = await service.mark_processing(call.id)
    assert processing.status == CallStatus.processing

    done = await service.mark_done(call.id, "removed")
    assert done.status == CallStatus.done
    assert done.end_reason == "removed"


async def test_mark_failed_from_every_non_terminal_state(service: CallService) -> None:
    queued = await service.create(make_payload())
    failed_queued = await service.mark_failed(queued.id, "concurrency_limit")
    assert failed_queued.status == CallStatus.failed
    assert failed_queued.failure_reason == "concurrency_limit"

    joining_call = await service.create(make_payload())
    await service.mark_bot_started(joining_call.id, "oreeai-bot-test")
    failed_joining = await service.mark_failed(joining_call.id, "never_admitted")
    assert failed_joining.status == CallStatus.failed

    recording_call = await service.create(make_payload())
    await service.mark_bot_started(recording_call.id, "oreeai-bot-test")
    await service.mark_recording(recording_call.id)
    failed_recording = await service.mark_failed(recording_call.id, "bot_error:crash")
    assert failed_recording.status == CallStatus.failed

    processing_call = await service.create(make_payload())
    await service.mark_bot_started(processing_call.id, "oreeai-bot-test")
    await service.mark_recording(processing_call.id)
    await service.mark_processing(processing_call.id)
    failed_processing = await service.mark_failed(processing_call.id, "transcription_failed:x")
    assert failed_processing.status == CallStatus.failed


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (CallStatus.queued, CallStatus.recording),
        (CallStatus.queued, CallStatus.processing),
        (CallStatus.queued, CallStatus.done),
        (CallStatus.joining, CallStatus.processing),
        (CallStatus.joining, CallStatus.done),
        (CallStatus.recording, CallStatus.done),
        (CallStatus.recording, CallStatus.joining),
        (CallStatus.processing, CallStatus.recording),
        (CallStatus.done, CallStatus.recording),
        (CallStatus.failed, CallStatus.queued),
    ],
)
async def test_illegal_transitions_are_rejected(
    service: CallService, start: CallStatus, target: CallStatus
) -> None:
    call = await service.create(make_payload())
    await _force_status(service, call.id, start)

    with pytest.raises(ConflictError, match="illegal transition"):
        await service.update_status(call.id, target)


async def test_terminal_states_reject_any_transition(service: CallService) -> None:
    done_call = await service.create(make_payload())
    await _force_status(service, done_call.id, CallStatus.done)
    with pytest.raises(ConflictError):
        await service.mark_recording(done_call.id)

    failed_call = await service.create(make_payload())
    await _force_status(service, failed_call.id, CallStatus.failed)
    with pytest.raises(ConflictError):
        await service.mark_failed(failed_call.id, "again")


async def test_update_status_sets_reasons(service: CallService) -> None:
    call = await service.create(make_payload())

    updated = await service.update_status(
        call.id, CallStatus.failed, failure_reason="never_admitted"
    )
    assert updated.failure_reason == "never_admitted"
    assert updated.end_reason is None


async def _force_status(service: CallService, call_id: uuid.UUID, status: CallStatus) -> None:
    if status == CallStatus.queued:
        return
    if status == CallStatus.joining:
        await service.mark_bot_started(call_id, "oreeai-bot-test")
        return
    if status == CallStatus.recording:
        await service.mark_bot_started(call_id, "oreeai-bot-test")
        await service.mark_recording(call_id)
        return
    if status == CallStatus.processing:
        await service.mark_bot_started(call_id, "oreeai-bot-test")
        await service.mark_recording(call_id)
        await service.mark_processing(call_id)
        return
    if status == CallStatus.done:
        await _force_status(service, call_id, CallStatus.processing)
        await service.mark_done(call_id, "call_ended")
        return
    if status == CallStatus.failed:
        await service.mark_failed(call_id, "never_admitted")
        return
