import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oreeai_nt.enums import MeetingPlatform, MeetingStatus


class UTCDatetimeModel(BaseModel):
    @field_validator("scheduled_at", "started_at", "ended_at", check_fields=False)
    @classmethod
    def _normalize_datetimes_to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class MeetingBase(UTCDatetimeModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    platform: MeetingPlatform = MeetingPlatform.other
    scheduled_at: datetime | None = None


class MeetingCreate(MeetingBase):
    pass


class MeetingUpdate(UTCDatetimeModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    platform: MeetingPlatform | None = None
    status: MeetingStatus | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: str | None = None
    summary: str | None = None


class MeetingListItem(UTCDatetimeModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None = None
    platform: MeetingPlatform
    status: MeetingStatus
    scheduled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


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
