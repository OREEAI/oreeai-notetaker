import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from oreeai_nt.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MeetingPlatform(enum.StrEnum):
    google_meet = "google_meet"
    zoom = "zoom"
    in_person = "in_person"
    other = "other"


class MeetingStatus(enum.StrEnum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class Meeting(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "meetings"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[MeetingPlatform] = mapped_column(
        Enum(MeetingPlatform, name="meeting_platform", native_enum=False, length=50),
        default=MeetingPlatform.other,
        nullable=False,
    )
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="meeting_status", native_enum=False, length=50),
        default=MeetingStatus.scheduled,
        nullable=False,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
