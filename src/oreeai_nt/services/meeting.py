import uuid
from datetime import datetime

from oreeai_nt.core.cache import CacheService
from oreeai_nt.core.exceptions import ConflictError, NotFoundError
from oreeai_nt.enums import MeetingStatus
from oreeai_nt.models.meeting import Meeting
from oreeai_nt.repositories.meeting import MeetingRepository
from oreeai_nt.schemas.meeting import MeetingCreate, MeetingListItem, MeetingRead, MeetingUpdate


class MeetingService:
    def __init__(self, repository: MeetingRepository, cache: CacheService) -> None:
        self._repository = repository
        self._cache = cache

    async def create_meeting(self, data: MeetingCreate) -> MeetingRead:
        meeting = Meeting(**data.model_dump())
        created = await self._repository.create(meeting)
        return MeetingRead.model_validate(created)

    async def get_meeting(self, meeting_id: uuid.UUID) -> MeetingRead:
        cache_key = self._cache.key("meeting", meeting_id)
        cached = await self._cache.get_json(cache_key)
        if cached is not None:
            return MeetingRead.model_validate(cached)

        meeting = await self._require_meeting(meeting_id)
        read = MeetingRead.model_validate(meeting)
        await self._cache.set_json(cache_key, read.model_dump(mode="json"))
        return read

    async def list_meetings(self, *, offset: int = 0, limit: int = 100) -> list[MeetingListItem]:
        meetings = await self._repository.list(offset=offset, limit=limit)
        return [MeetingListItem.model_validate(m) for m in meetings]

    async def update_meeting(self, meeting_id: uuid.UUID, data: MeetingUpdate) -> MeetingRead:
        meeting = await self._require_meeting(meeting_id)
        changes = data.model_dump(exclude_unset=True)

        status = changes.get("status", meeting.status)
        started_at = changes.get("started_at", meeting.started_at)
        ended_at = changes.get("ended_at", meeting.ended_at)
        self._validate_times(status, started_at, ended_at)

        updated = await self._repository.update(meeting, changes)
        await self._invalidate(meeting_id)
        return MeetingRead.model_validate(updated)

    async def delete_meeting(self, meeting_id: uuid.UUID) -> None:
        meeting = await self._require_meeting(meeting_id)
        await self._repository.delete(meeting)
        await self._invalidate(meeting_id)

    async def _require_meeting(self, meeting_id: uuid.UUID) -> Meeting:
        meeting = await self._repository.get(meeting_id)
        if meeting is None:
            raise NotFoundError(f"Meeting {meeting_id} not found")
        return meeting

    async def _invalidate(self, meeting_id: uuid.UUID) -> None:
        await self._cache.delete(self._cache.key("meeting", meeting_id))

    @staticmethod
    def _validate_times(
        status: MeetingStatus, started_at: datetime | None, ended_at: datetime | None
    ) -> None:
        if status == MeetingStatus.completed and ended_at is None:
            raise ConflictError("ended_at is required to complete a meeting")
        if started_at is not None and ended_at is not None and ended_at < started_at:
            raise ConflictError("ended_at cannot be before started_at")
