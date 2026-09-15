# ruff: noqa: ASYNC240
"""Retention sweep tests (PR 6 pass 2).

Mirrors of "You test this" scenarios 3 and 4 that CI allows: done/failed
cutoffs, non-terminal statuses untouched, missing-object resilience,
per-row error continuation, and the log-hygiene rule (no user_ref, no
webhook_secret, no object key in retention log lines).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import oreeai_notetaker.workers.retention as retention
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.base import Base
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.integrations.object_storage.base import DeleteFailed
from oreeai_notetaker.models.call import Call

SECRET = "shhhhhhhhhhhhhhhh"
TRANSCRIPT = [{"speaker": "S0", "start": 0.0, "end": 1.2, "text": "hello"}]


class FakeStorage:
    """``ObjectStorageService`` stand-in recording deletes; raises
    ``DeleteFailed`` for calls in ``fail_for`` (transient provider error)."""

    def __init__(self) -> None:
        self.deleted: list[uuid.UUID] = []
        self.fail_for: set[uuid.UUID] = set()

    async def upload_for_call(self, call_id: uuid.UUID, file_path: Path) -> str:
        return f"s3://test-bucket/calls/{call_id}/audio.wav"

    async def delete_for_call(self, call_id: uuid.UUID) -> None:
        if call_id in self.fail_for:
            raise DeleteFailed(f"delete failed for call {call_id}")
        self.deleted.append(call_id)

    async def presigned_url_for_call(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        return f"file:///scratch/calls/{call_id}/audio.wav"


@pytest.fixture
async def retention_db() -> AsyncIterator[async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def patch_retention_db(
    retention_db: async_sessionmaker[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "session_factory", retention_db)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


def db() -> async_sessionmaker[Any]:
    return retention.session_factory  # type: ignore[return-value]


async def make_call(
    status: CallStatus,
    *,
    audio_url: str | None = None,
    age_days: float | None = None,
    **extra: Any,
) -> Call:
    async with db()() as session:
        call = Call(
            meeting_url="https://meet.google.com/abc-defg-hij",
            user_ref="retention-user-1",
            consent_ack=True,
            webhook_url="http://receiver.example/hooks/x",
            webhook_secret=SECRET,
        )
        call.status = status
        call.audio_url = audio_url
        call.transcript = TRANSCRIPT if status == CallStatus.done else None
        if age_days is not None:
            call.updated_at = datetime.now(UTC) - timedelta(days=age_days)
        for key, value in extra.items():
            setattr(call, key, value)
        session.add(call)
        await session.commit()
        return call


async def get_call(call_id: uuid.UUID) -> Call:
    async with db()() as session:
        call = await session.get(Call, call_id)
        assert call is not None
        return call


def with_audio(call_id: uuid.UUID) -> str:
    return f"s3://test-bucket/calls/{call_id}/audio.wav"


class TestDoneRetention:
    async def test_zero_days_deletes_immediately(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        call = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))

        reclaimed = await retention.enforce_retention(storage)

        assert reclaimed == 1
        assert storage.deleted == [call.id]
        fresh = await get_call(call.id)
        assert fresh.audio_url is None
        assert fresh.transcript == TRANSCRIPT, "transcript is never touched"
        assert fresh.status == CallStatus.done

    async def test_inside_cutoff_with_one_day_survives(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 1)
        call = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))

        await retention.enforce_retention(storage)

        fresh = await get_call(call.id)
        assert fresh.audio_url is not None
        assert storage.deleted == []

    async def test_past_cutoff_with_one_day_reclaimed(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 1)
        call = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()), age_days=2)

        await retention.enforce_retention(storage)

        assert storage.deleted == [call.id]
        fresh = await get_call(call.id)
        assert fresh.audio_url is None


class TestFailedRetention:
    async def test_default_seven_days_keeps_recent(self, storage: FakeStorage) -> None:
        call = await make_call(CallStatus.failed, audio_url=with_audio(uuid.uuid4()))

        await retention.enforce_retention(storage)

        fresh = await get_call(call.id)
        assert fresh.audio_url is not None
        assert storage.deleted == []

    async def test_zero_days_reclaims(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "failed_audio_retention_days", 0)
        call = await make_call(CallStatus.failed, audio_url=with_audio(uuid.uuid4()))

        reclaimed = await retention.enforce_retention(storage)

        assert reclaimed == 1
        assert storage.deleted == [call.id]
        fresh = await get_call(call.id)
        assert fresh.audio_url is None


class TestUntouchedStatuses:
    async def test_recording_never_touched(self, storage: FakeStorage) -> None:
        call = await make_call(
            CallStatus.recording, audio_url=with_audio(uuid.uuid4()), age_days=30
        )

        await retention.enforce_retention(storage)

        fresh = await get_call(call.id)
        assert fresh.audio_url is not None
        assert storage.deleted == []

    async def test_non_terminal_statuses_never_touched(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        monkeypatch.setattr(settings, "failed_audio_retention_days", 0)
        calls = [
            await make_call(status, audio_url=with_audio(uuid.uuid4()), age_days=30)
            for status in (CallStatus.queued, CallStatus.joining, CallStatus.processing)
        ]

        await retention.enforce_retention(storage)

        assert storage.deleted == []
        for call in calls:
            fresh = await get_call(call.id)
            assert fresh.audio_url is not None

    async def test_done_without_audio_url_ignored(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        await make_call(CallStatus.done)  # no audio_url: nothing to reclaim

        reclaimed = await retention.enforce_retention(storage)

        assert reclaimed == 0
        assert storage.deleted == []


class TestResilience:
    async def test_missing_object_does_not_crash_sweep(
        self, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FakeStorage's delete is a silent no-op: the missing-object case
        # (object already gone provider-side) must still null audio_url.
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        call = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))

        reclaimed = await retention.enforce_retention(storage)

        assert reclaimed == 1
        fresh = await get_call(call.id)
        assert fresh.audio_url is None

    async def test_delete_failure_continues_to_next_row(
        self,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        stuck = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))
        healthy = await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))
        storage.fail_for.add(stuck.id)

        reclaimed = await retention.enforce_retention(storage)

        assert reclaimed == 1
        assert storage.deleted == [healthy.id]
        assert (await get_call(stuck.id)).audio_url is not None, "failed row retried next tick"
        assert (await get_call(healthy.id)).audio_url is None
        assert "retention could not reclaim" in caplog.text
        assert "user_ref" not in caplog.text
        assert SECRET not in caplog.text
        assert "audio.wav" not in caplog.text

    async def test_misconfigured_storage_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from oreeai_notetaker.integrations.object_storage.base import ConfigurationError

        def broken_build() -> Any:
            raise ConfigurationError("S3_BUCKET is not configured")

        monkeypatch.setattr(retention, "build_object_storage_service", broken_build)

        reclaimed = await retention.enforce_retention()

        assert reclaimed == 0

    async def test_log_hygiene_across_the_sweep(
        self,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "audio_retention_days", 0)
        await make_call(CallStatus.done, audio_url=with_audio(uuid.uuid4()))

        await retention.enforce_retention(storage)

        assert "user_ref" not in caplog.text
        assert "retention-user-1" not in caplog.text
        assert SECRET not in caplog.text
        assert "audio.wav" not in caplog.text
