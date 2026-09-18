"""Transcription-in-runner seam tests (PR 7 pass 2).

The chunk's scenario mirrors: happy path (bot exit 0 → upload →
transcribe → ``done`` with the carried ``end_reason`` and real
segments), permanent provider error → ``transcription_failed:``,
the size-guard mirror, the source-unavailable mirror, transient
exhaustion through the real service, and the now-live failed-with-audio
retention semantics (``FAILED_AUDIO_RETENTION_DAYS`` reclaims
transcription-failed audio at the window cutoff — the sweep keys off
``status`` + ``updated_at`` + ``audio_url IS NOT NULL`` only).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import oreeai_notetaker.workers.bot_runner as br
from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.base import Base
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
    SourceUnavailable,
)
from oreeai_notetaker.integrations.transcription.base import (
    AudioTooLarge,
    PermanentTranscriptionError,
    SpeakerSegment,
    Transcript,
    TransientTranscriptionError,
)
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.services.storage import reset_object_storage_service
from oreeai_notetaker.services.transcription import (
    TranscriptionService,
    reset_transcription_service,
)

SECRET = "shhhhhhhhhhhhhhhh"

FAKE_SEGMENTS = [
    {"speaker": "S0", "start": 0.1, "end": 1.4, "text": "hello there."},
    {"speaker": "S1", "start": 1.8, "end": 2.9, "text": "hi back"},
]


class FakeStorage:
    def __init__(self) -> None:
        self.staged: set[uuid.UUID] = set()
        self.uploads: list[uuid.UUID] = []
        self.source_fail = False

    def stage(self, call_id: uuid.UUID) -> None:
        self.staged.add(call_id)

    async def upload_for_call(self, call_id: uuid.UUID, file_path: Path) -> str:
        if call_id not in self.staged:
            raise AssertionError("unreachable: WAV existence is checked first")
        self.uploads.append(call_id)
        return f"s3://test-bucket/calls/{call_id}/audio.wav"

    async def delete_for_call(self, call_id: uuid.UUID) -> None:
        return None

    async def presigned_url_for_call(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        return f"file:///scratch/calls/{call_id}/audio.wav"

    async def transcribable_source_for_call(
        self, call_id: uuid.UUID, *, ttl_seconds: int | None = None
    ) -> AudioSource:
        if self.source_fail:
            raise SourceUnavailable(f"stored audio unavailable for call {call_id}")
        return AudioSource(url=f"https://s3.test/calls/{call_id}/audio.wav", size_bytes=1024)


class ScriptedTranscription:
    """``TranscriptionService``-shaped stand-in; raises what's scripted."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[AudioSource] = []
        self.error = error

    async def transcribe(self, source: AudioSource) -> Transcript:
        self.calls.append(source)
        if self.error is not None:
            raise self.error
        return Transcript(segments=[SpeakerSegment(**s) for s in FAKE_SEGMENTS])


@pytest.fixture
async def runner_db() -> AsyncIterator[async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def patch_runner_db(runner_db: async_sessionmaker[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(br, "session_factory", runner_db)


@pytest.fixture(autouse=True)
def reset_singletons() -> AsyncIterator[None]:
    reset_object_storage_service()
    reset_transcription_service()
    yield
    reset_object_storage_service()
    reset_transcription_service()


@pytest.fixture(autouse=True)
def dispatched_calls(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []

    async def fake_dispatch(call_id: uuid.UUID) -> None:
        seen.append(call_id)

    monkeypatch.setattr(br, "dispatch_webhook", fake_dispatch)
    return seen


@pytest.fixture(autouse=True)
def silent_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_retention() -> None:
        return None

    monkeypatch.setattr(br, "_run_retention_sweep", no_retention)


@pytest.fixture
def cache() -> CacheService:
    return CacheService(None)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def transcription() -> ScriptedTranscription:
    return ScriptedTranscription()


async def make_call(status: CallStatus = CallStatus.recording, **extra: Any) -> Call:
    async with runner_db_factory()() as session:
        call = Call(
            meeting_url="https://meet.google.com/abc-defg-hij",
            user_ref="test-user-1",
            consent_ack=True,
            webhook_url="http://receiver.example/hooks/x",
            webhook_secret=SECRET,
        )
        call.status = status
        for key, value in extra.items():
            setattr(call, key, value)
        session.add(call)
        await session.commit()
        return call


def runner_db_factory() -> async_sessionmaker[Any]:
    return br.session_factory  # type: ignore[return-value]


async def get_call(call_id: uuid.UUID) -> Call:
    async with runner_db_factory()() as session:
        call = await session.get(Call, call_id)
        assert call is not None
        return call


class TestTranscriptionSeam:
    async def test_happy_path_done_with_real_segments_and_carried_end_reason(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        transcription = ScriptedTranscription()
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "give_up"}, cache, storage, transcription
        )

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.done
        assert fresh.end_reason == "give_up"  # carried from the bot exit
        assert fresh.transcript == FAKE_SEGMENTS
        assert fresh.audio_url == f"s3://test-bucket/calls/{call.id}/audio.wav"
        assert not (tmp_path / f"{call.id}.wav").exists()
        assert fresh.id in dispatched_calls
        # the source the storage adapter built reached the provider client
        assert transcription.calls == [
            AudioSource(url=f"https://s3.test/calls/{call.id}/audio.wav", size_bytes=1024)
        ]

    async def test_permanent_error_marks_transcription_failed(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        transcription = ScriptedTranscription(
            error=PermanentTranscriptionError(
                "deepgram returned HTTP 402 (insufficient credits — operator action required)"
            )
        )
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason.startswith("transcription_failed:")
        assert "402" in fresh.failure_reason
        assert fresh.audio_url is not None
        assert fresh.transcript is None
        assert fresh.end_reason is None, "done never reached"
        assert not (tmp_path / f"{call.id}.wav").exists(), (
            "scratch WAV deleted (the stored copy is the durable record)"
        )
        assert fresh.id in dispatched_calls

    async def test_size_guard_marks_failed_with_size_reason(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        # Manual scenario mirror: AUDIO_MAX_BYTES=1000 → failed with a size reason
        transcription = ScriptedTranscription(
            error=AudioTooLarge("audio too large: 2000 bytes > AUDIO_MAX_BYTES (1000)")
        )
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason.startswith("transcription_failed:")
        assert "2000 bytes" in fresh.failure_reason
        assert fresh.audio_url is not None
        assert fresh.id in dispatched_calls

    async def test_source_unavailable_marks_failed_and_webhooks(
        self,
        cache: CacheService,
        storage: FakeStorage,
        transcription: ScriptedTranscription,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        storage.source_fail = True
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "transcription_failed:source_unavailable"
        assert transcription.calls == [], "provider never called without a source"
        assert fresh.id in dispatched_calls

    async def test_transient_exhaustion_through_real_service_marks_failed(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        class ExhaustClient:
            def __init__(self) -> None:
                self.calls = 0

            async def transcribe(self, source: AudioSource) -> Transcript:
                self.calls += 1
                raise TransientTranscriptionError("deepgram returned HTTP 429")

        client = ExhaustClient()

        async def no_sleep(seconds: float) -> None:
            return None

        transcription = TranscriptionService(client, sleeper=no_sleep)  # type: ignore[arg-type]
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        fresh = await get_call(call.id)
        assert client.calls == 3, "service retry policy owns attempt count"
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason.startswith("transcription_failed:")
        assert "3 attempts" in fresh.failure_reason
        assert fresh.id in dispatched_calls

    async def test_misconfigured_lazy_build_degrades_to_failed(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        """transcription=None forces the lazy build; a broken configuration
        must degrade to transcription_failed, never strand the call."""
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))

        def broken_build() -> Any:
            raise ConfigurationError("DEEPGRAM_API_KEY is not configured")

        monkeypatch.setattr(br, "build_transcription_service", broken_build)
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache, storage)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason.startswith("transcription_failed:misconfigured:")
        assert fresh.id in dispatched_calls

    async def test_transcription_failure_logs_never_carry_user_ref_or_text(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transcription = ScriptedTranscription(
            error=PermanentTranscriptionError("deepgram returned HTTP 401")
        )
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        assert str(call.id) in caplog.text
        assert "user_ref" not in caplog.text
        assert "test-user-1" not in caplog.text
        assert SECRET not in caplog.text
        # no transcript text (the error path carries none by construction)
        assert "hello there." not in caplog.text

    async def test_done_log_carries_segment_count_only(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transcription = ScriptedTranscription()
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        import logging as _logging

        caplog.set_level(_logging.INFO, logger="oreeai.runner")
        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )

        assert f"segments={len(FAKE_SEGMENTS)}" in caplog.text
        assert "hello there." not in caplog.text, "never the text, only the count"


class TestFailedWithAudioRetention:
    async def test_failed_with_audio_reclaimed_at_cutoff(
        self,
        cache: CacheService,
        storage: FakeStorage,
        runner_db: async_sessionmaker[Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        """FAILED_AUDIO_RETENTION_DAYS becomes live in PR 7: upload
        succeeds → transcription permanently fails → failed WITH audio,
        reclaimed by the sweep at the window cutoff (unit mirror; the
        real-provider validation is deferred to creds)."""
        transcription = ScriptedTranscription(
            error=PermanentTranscriptionError("deepgram returned HTTP 402")
        )
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        monkeypatch.setattr(settings, "failed_audio_retention_days", 0)
        call = await make_call(bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        await br.apply_exit_status(
            call.id, 0, {"end_reason": "call_ended"}, cache, storage, transcription
        )
        assert (await get_call(call.id)).audio_url is not None

        # the real retention worker (not the runner's stubbed sweep) owns
        # reclaim: cutoff 0 → failed row with audio_url is eligible now;
        # the local adapter's delete is idempotent, the row keeps its
        # transcript. Transcript column None here (never reached done).
        from oreeai_notetaker.workers import retention as retention_mod

        monkeypatch.setattr(retention_mod, "session_factory", runner_db)
        await retention_mod.enforce_retention()

        fresh = await get_call(call.id)
        assert fresh.audio_url is None
        assert fresh.status == CallStatus.failed


class TestStaleProcessingSweep:
    async def test_stale_processing_call_fails_and_webhooks(
        self,
        cache: CacheService,
        monkeypatch: pytest.MonkeyPatch,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        call = await make_call(CallStatus.processing)
        # backdate past PROCESSING_STALE_CUTOFF_S (explicit value prevents
        # the onupdate default from refreshing updated_at)
        async with runner_db_factory()() as session:
            await session.execute(
                Call.__table__.update()
                .where(Call.id == call.id)
                .values(
                    updated_at=datetime.now(UTC)
                    - timedelta(seconds=br.PROCESSING_STALE_CUTOFF_S + 1)
                )
            )
            await session.commit()

        container_ops: list[str] = []
        monkeypatch.setattr(br, "run_docker", _recording_docker(container_ops))

        await br.stale_sweep(cache)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "stale_call"
        assert fresh.id in dispatched_calls
        assert len(container_ops) == 2, "kill + rm attempted (best-effort; container already gone)"

    async def test_fresh_processing_call_not_swept(
        self,
        cache: CacheService,
        monkeypatch: pytest.MonkeyPatch,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        call = await make_call(CallStatus.processing)  # updated_at = now

        async def boom(*args: Any) -> tuple[int, str, str]:
            raise AssertionError("no docker ops for a fresh processing call")

        monkeypatch.setattr(br, "run_docker", boom)
        await br.stale_sweep(cache)
        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.processing
        assert fresh.id not in dispatched_calls


def _recording_docker(calls: list[str]) -> Any:
    async def fake_run_docker(*args: str) -> tuple[int, str, str]:
        calls.append(" ".join(args))
        return 0, "", ""

    return fake_run_docker
