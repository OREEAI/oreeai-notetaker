import uuid

from fastapi import APIRouter, Query

from oreeai_nt.api.deps import MeetingServiceDep
from oreeai_nt.schemas.meeting import MeetingCreate, MeetingListItem, MeetingRead, MeetingUpdate

router = APIRouter()


@router.post("", response_model=MeetingRead, status_code=201)
async def create_meeting(data: MeetingCreate, service: MeetingServiceDep) -> MeetingRead:
    return await service.create_meeting(data)


@router.get("", response_model=list[MeetingListItem])
async def list_meetings(
    service: MeetingServiceDep,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[MeetingListItem]:
    return await service.list_meetings(offset=offset, limit=limit)


@router.get("/{meeting_id}", response_model=MeetingRead)
async def get_meeting(meeting_id: uuid.UUID, service: MeetingServiceDep) -> MeetingRead:
    return await service.get_meeting(meeting_id)


@router.patch("/{meeting_id}", response_model=MeetingRead)
async def update_meeting(
    meeting_id: uuid.UUID, data: MeetingUpdate, service: MeetingServiceDep
) -> MeetingRead:
    return await service.update_meeting(meeting_id, data)


@router.delete("/{meeting_id}", status_code=204)
async def delete_meeting(meeting_id: uuid.UUID, service: MeetingServiceDep) -> None:
    await service.delete_meeting(meeting_id)
