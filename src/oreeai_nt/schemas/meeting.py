import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oreeai_nt.models.meeting import MeetingPlatform, MeetingStatus


class MeetingBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    platform: MeetingPlatform = MeetingPlatform.other
    scheduled_at: datetime | None = None


class MeetingCreate(MeetingBase):
    pass


class MeetingUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    platform: MeetingPlatform | None = None
    status: MeetingStatus | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: str | None = None
    summary: str | None = None


class MeetingRead(MeetingBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: MeetingStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: str | None = None
    summary: str | None = None
    created_at: datetime
    updated_at: datetime
