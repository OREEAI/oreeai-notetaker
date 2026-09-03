import uuid

import pytest

from oreeai_nt.core.exceptions import ConflictError, NotFoundError
from oreeai_nt.repositories.meeting import MeetingRepository
from oreeai_nt.schemas.meeting import MeetingCreate, MeetingUpdate
from oreeai_nt.services.meeting import MeetingService


@pytest.fixture
def service(db_session, fake_cache) -> MeetingService:
    return MeetingService(MeetingRepository(db_session), fake_cache)


async def test_create_and_get(db_session, service) -> None:
    created = await service.create_meeting(MeetingCreate(title="Standup"))
    fetched = await service.get_meeting(created.id)
    assert fetched.id == created.id
    assert fetched.status == "scheduled"


async def test_get_missing_raises(service) -> None:
    with pytest.raises(NotFoundError):
        await service.get_meeting(uuid.uuid4())


async def test_update_validation(service) -> None:
    created = await service.create_meeting(MeetingCreate(title="Standup"))

    with pytest.raises(ConflictError):
        await service.update_meeting(created.id, MeetingUpdate(status="completed"))

    with pytest.raises(ConflictError):
        await service.update_meeting(
            created.id,
            MeetingUpdate(
                started_at="2026-09-10T11:00:00Z",
                ended_at="2026-09-10T10:00:00Z",
            ),
        )


async def test_delete_missing_raises(service) -> None:
    with pytest.raises(NotFoundError):
        await service.delete_meeting(uuid.uuid4())
