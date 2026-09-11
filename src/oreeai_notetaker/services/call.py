import uuid
from typing import Any

from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.core.exceptions import ConflictError, NotFoundError
from oreeai_notetaker.enums import CallPlatform, CallStatus
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.repositories.call import CallRepository
from oreeai_notetaker.schemas.call import CallCreate, CallRead

_LEGAL_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.queued: frozenset({CallStatus.joining, CallStatus.failed}),
    CallStatus.joining: frozenset({CallStatus.recording, CallStatus.failed}),
    CallStatus.recording: frozenset({CallStatus.processing, CallStatus.failed}),
    CallStatus.processing: frozenset({CallStatus.done, CallStatus.failed}),
}


class CallService:
    """Call lifecycle service.

    Reads intentionally bypass ``CacheService``: call statuses are hot and
    cached reads would go stale between runner transitions.
    """

    def __init__(self, repository: CallRepository, cache: CacheService) -> None:
        self._repository = repository
        self._cache = cache

    async def create(self, data: CallCreate) -> CallRead:
        active = await self._repository.count_active()
        if active >= settings.call_concurrency_limit:
            raise ConflictError("CALL_CONCURRENCY_LIMIT reached")

        call = Call(
            meeting_url=data.meeting_url,
            user_ref=data.user_ref,
            consent_ack=data.consent_ack,
            platform=data.platform or CallPlatform.google_meet,
            status=CallStatus.queued,
            webhook_url=str(data.webhook_url),
            webhook_secret=data.webhook_secret,
        )
        created = await self._repository.create(call)
        return CallRead.model_validate(created)

    async def get(self, call_id: uuid.UUID) -> CallRead:
        call = await self._require_call(call_id)
        return CallRead.model_validate(call)

    async def update_status(
        self,
        call_id: uuid.UUID,
        new_status: CallStatus,
        *,
        end_reason: str | None = None,
        failure_reason: str | None = None,
    ) -> CallRead:
        call = await self._require_call(call_id)
        changes: dict[str, Any] = {}
        if end_reason is not None:
            changes["end_reason"] = end_reason
        if failure_reason is not None:
            changes["failure_reason"] = failure_reason
        updated = await self._transition(call, new_status, changes=changes)
        return CallRead.model_validate(updated)

    async def mark_bot_started(self, call_id: uuid.UUID, container_name: str) -> CallRead:
        call = await self._require_call(call_id)
        updated = await self._transition(
            call, CallStatus.joining, changes={"bot_container_name": container_name}
        )
        return CallRead.model_validate(updated)

    async def mark_recording(self, call_id: uuid.UUID) -> CallRead:
        call = await self._require_call(call_id)
        updated = await self._transition(call, CallStatus.recording)
        return CallRead.model_validate(updated)

    async def mark_processing(self, call_id: uuid.UUID) -> CallRead:
        call = await self._require_call(call_id)
        updated = await self._transition(call, CallStatus.processing)
        return CallRead.model_validate(updated)

    async def mark_done(
        self,
        call_id: uuid.UUID,
        end_reason: str,
        *,
        transcript: list[dict[str, Any]] | None = None,
    ) -> CallRead:
        call = await self._require_call(call_id)
        changes: dict[str, Any] = {"end_reason": end_reason}
        if transcript is not None:
            changes["transcript"] = transcript
        updated = await self._transition(call, CallStatus.done, changes=changes)
        return CallRead.model_validate(updated)

    async def mark_failed(self, call_id: uuid.UUID, failure_reason: str) -> CallRead:
        call = await self._require_call(call_id)
        updated = await self._transition(
            call, CallStatus.failed, changes={"failure_reason": failure_reason}
        )
        return CallRead.model_validate(updated)

    async def _require_call(self, call_id: uuid.UUID) -> Call:
        call = await self._repository.get(call_id)
        if call is None:
            raise NotFoundError(f"Call {call_id} not found")
        return call

    async def _transition(
        self,
        call: Call,
        new_status: CallStatus,
        *,
        changes: dict[str, Any] | None = None,
    ) -> Call:
        allowed = _LEGAL_TRANSITIONS.get(call.status, frozenset())
        if new_status not in allowed:
            raise ConflictError(f"illegal transition {call.status.value} -> {new_status.value}")
        payload: dict[str, Any] = {"status": new_status}
        if changes:
            payload.update(changes)
        return await self._repository.update(call, payload)
