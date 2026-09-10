from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from oreeai_notetaker.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from oreeai_notetaker.enums import ACTIVE_STATUSES, CallPlatform, CallStatus

_ACTIVE_STATUS_SQL = ", ".join(f"'{status.value}'" for status in ACTIVE_STATUSES)


class Call(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "calls"
    __table_args__ = (
        Index(
            "ix_calls_status_active",
            "status",
            postgresql_where=text(f"status IN ({_ACTIVE_STATUS_SQL})"),
        ),
    )

    meeting_url: Mapped[str] = mapped_column(Text, nullable=False)
    user_ref: Mapped[str] = mapped_column(Text, nullable=False)
    consent_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    platform: Mapped[CallPlatform] = mapped_column(
        Enum(CallPlatform, name="call_platform", native_enum=False, length=50),
        default=CallPlatform.google_meet,
        nullable=False,
    )
    status: Mapped[CallStatus] = mapped_column(
        Enum(CallStatus, name="call_status", native_enum=False, length=50),
        default=CallStatus.queued,
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    webhook_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bot_container_name: Mapped[str | None] = mapped_column(Text, nullable=True)
