import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from pydantic_core import PydanticCustomError

from oreeai_notetaker.enums import CallPlatform, CallStatus

MEETING_URL_PATTERN = re.compile(r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(\?.*)?$")

CONTRACT_VALIDATION_CODES: frozenset[str] = frozenset({"consent_missing", "invalid_meeting_url"})

MIN_WEBHOOK_SECRET_LENGTH = 16


class CallCreate(BaseModel):
    meeting_url: str
    user_ref: str = Field(min_length=1)
    consent_ack: bool = False
    webhook_url: HttpUrl
    webhook_secret: str = Field(min_length=MIN_WEBHOOK_SECRET_LENGTH)
    platform: CallPlatform | None = None

    @field_validator("meeting_url")
    @classmethod
    def _validate_meeting_url(cls, value: str) -> str:
        if MEETING_URL_PATTERN.fullmatch(value) is None:
            raise PydanticCustomError("invalid_meeting_url", "invalid_meeting_url")
        return value

    @model_validator(mode="after")
    def _require_consent(self) -> "CallCreate":
        if not self.consent_ack:
            raise PydanticCustomError("consent_missing", "consent_missing")
        return self

    @model_validator(mode="after")
    def _derive_platform(self) -> "CallCreate":
        if self.platform is None:
            self.platform = CallPlatform.google_meet
        elif self.platform != CallPlatform.google_meet:
            raise PydanticCustomError("invalid_meeting_url", "invalid_meeting_url")
        return self


class CallCreated(BaseModel):
    call_id: uuid.UUID
    status: CallStatus


class CallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_url: str
    user_ref: str
    consent_ack: bool
    platform: CallPlatform
    status: CallStatus
    failure_reason: str | None = None
    end_reason: str | None = None
    audio_url: str | None = None
    transcript: list[dict[str, Any]] | None = None
    webhook_url: str
    webhook_delivered_at: datetime | None = None
    webhook_attempts: int
    bot_container_name: str | None = None
    created_at: datetime
    updated_at: datetime
