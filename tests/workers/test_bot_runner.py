# ruff: noqa: ASYNC240
import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import oreeai_notetaker.workers.bot_runner as br
from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.base import Base
from oreeai_notetaker.enums import ACTIVE_STATUSES, CallStatus
from oreeai_notetaker.integrations.object_storage.base import (
    ConfigurationError,
    UploadFailed,
)
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.services.storage import reset_object_storage_service

SECRET = "shhhhhhhhhhhhhhhh"


class FakeStorage:
    """``ObjectStorageService`` stand-in for the runner seam: records
    uploads and raises ``UploadFailed`` when flagged (or when the call
    was never staged, which the real WAV-existence check guards)."""

    def __init__(self) -> None:
        self.staged: set[uuid.UUID] = set()
        self.uploads: list[uuid.UUID] = []
        self.fail = False

    def stage(self, call_id: uuid.UUID) -> None:
        self.staged.add(call_id)

    def reset(self) -> None:
        self.staged.clear()
        self.uploads.clear()
        self.fail = False

    async def upload_for_call(self, call_id: uuid.UUID, file_path: Path) -> str:
        if self.fail or call_id not in self.staged:
            raise UploadFailed(f"s3 upload failed for call {call_id}")
        self.uploads.append(call_id)
        return f"s3://test-bucket/calls/{call_id}/audio.wav"

    async def delete_for_call(self, call_id: uuid.UUID) -> None:
        return None

    async def presigned_url_for_call(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        return f"file:///scratch/calls/{call_id}/audio.wav"


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


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
def fresh_storage_singleton() -> None:
    reset_object_storage_service()
    yield
    reset_object_storage_service()


@pytest.fixture(autouse=True)
def silent_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retention behavior is covered by tests/workers/test_retention.py;
    the immediate post-done sweep fires inside these runner tests."""

    async def no_retention() -> None:
        return None

    monkeypatch.setattr(br, "_run_retention_sweep", no_retention)


@pytest.fixture(autouse=True)
def dispatched_calls(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []

    async def fake_dispatch(call_id: uuid.UUID) -> None:
        seen.append(call_id)

    monkeypatch.setattr(br, "dispatch_webhook", fake_dispatch)
    return seen


@pytest.fixture
def cache() -> CacheService:
    return CacheService(None)


async def make_call(status: CallStatus = CallStatus.queued, **extra: Any) -> Call:
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


def make_spawn_args_call(call_id: uuid.UUID) -> Call:
    call = Call(
        meeting_url="https://meet.google.com/abc-defg-hij",
        user_ref="test-user-1",
        consent_ack=True,
        webhook_url="http://receiver.example/hooks/x",
        webhook_secret=SECRET,
    )
    call.id = call_id
    return call


def compose_service_text(path: str, service: str) -> str:
    lines = Path(path).read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {service}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):
            end = i
            break
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            end = i
            break
    return "\n".join(lines[start:end])


class TestSpawnArgs:
    def test_envelope_flags_pinned(self) -> None:
        args = br.build_spawn_args(make_spawn_args_call(uuid.uuid4()))
        assert args[:2] == ["docker", "run"]
        assert "--rm" in args
        assert "--init" in args
        assert "--shm-size=1g" in args
        assert "--memory=2g" in args
        assert "--cpus=1.5" in args
        assert "--pids-limit=512" in args
        assert "--log-driver" in args and "json-file" in args
        assert "max-size=10m" in args
        assert "max-file=3" in args
        assert not any(a.startswith("--restart") for a in args)
        assert "-d" not in args

    def test_env_consent_and_call_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOT_AUTH_MODE", raising=False)
        call_id = uuid.uuid4()
        args = br.build_spawn_args(make_spawn_args_call(call_id))
        assert "MEETING_URL=https://meet.google.com/abc-defg-hij" in args
        assert f"CALL_ID={call_id}" in args
        assert "CONSENT_ACK=true" in args

    def test_env_passthrough_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_AUTH_MODE", "authenticated")
        monkeypatch.setenv("BOT_MAX_RECORD_DURATION", "10800")
        args = br.build_spawn_args(make_spawn_args_call(uuid.uuid4()))
        assert "BOT_AUTH_MODE=authenticated" in args
        assert "BOT_MAX_RECORD_DURATION=10800" in args
        assert not any("user_ref" in a.lower() for a in args)

    def test_volumes_network_image(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path / "audio"))
        monkeypatch.setattr(settings, "bot_docker_network", "testnet")
        monkeypatch.setattr(settings, "bot_image_tag", "1.2.3")
        args = br.build_spawn_args(make_spawn_args_call(uuid.uuid4()))
        assert "--network" in args and "testnet" in args
        assert f"{tmp_path / 'audio'}:/audio" in args
        assert args[-1] == "oreeai-bot:1.2.3"

    def test_profile_mount(self, tmp_path: Path) -> None:
        args = br.build_spawn_args(make_spawn_args_call(uuid.uuid4()), str(tmp_path))
        assert f"{tmp_path}:/profile" in args
        assert "BOT_PROFILE_DIR=/profile" in args

    def test_command_override_for_tests(self) -> None:
        args = br.build_spawn_args(
            make_spawn_args_call(uuid.uuid4()), image="busybox", command=["sleep", "5"]
        )
        assert args[-3:] == ["busybox", "sleep", "5"]


class TestComposeParity:
    def test_bot_service_envelope_matches_runner_constants(self) -> None:
        bot = compose_service_text("bot/docker-compose.yml", "bot")
        assert "mem_limit: 2g" in bot
        assert 'cpus: "1.5"' in bot
        assert "pids_limit: 512" in bot
        assert "restart: on-failure:2" in bot
        assert 'max-size: "10m"' in bot
        assert 'max-file: "3"' in bot
        assert "/var/lib/oreeai/audio:/audio" in bot
        assert 'CONSENT_ACK: "true"' in bot
        assert "ports:" not in bot
        assert "expose:" not in bot

    def test_runner_service_declared(self) -> None:
        runner = compose_service_text("bot/docker-compose.yml", "bot-runner")
        assert "oreeai_notetaker.workers.bot_runner" in runner
        assert "docker.sock" in runner
        assert "/var/lib/oreeai/audio:/var/lib/oreeai/audio" in runner
        assert "ports:" not in runner


class TestExitMapping:
    async def test_table(
        self,
        cache: CacheService,
        dispatched_calls: list[uuid.UUID],
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        cases = [
            (CallStatus.recording, 0, {"end_reason": "call_ended"}, CallStatus.done, "call_ended"),
            (CallStatus.recording, 0, {"end_reason": "give_up"}, CallStatus.done, "give_up"),
            (CallStatus.recording, 3, None, CallStatus.done, "removed"),
            (CallStatus.recording, 7, {"end_reason": "alone"}, CallStatus.done, "alone"),
            (CallStatus.recording, 2, None, CallStatus.failed, "never_admitted"),
            (CallStatus.recording, 4, None, CallStatus.failed, "no_show"),
            (CallStatus.joining, 4, None, CallStatus.failed, "join_timeout"),
            (CallStatus.recording, 5, None, CallStatus.failed, "bot_error:exit_5"),
            (CallStatus.recording, 6, None, CallStatus.failed, "consent_missing"),
            (CallStatus.recording, 7, None, CallStatus.failed, "silent_recording"),
            (CallStatus.recording, 99, None, CallStatus.failed, "bot_error:exit_99"),
        ]
        for start, exit_code, result, expected_status, reason in cases:
            storage.reset()
            call = await make_call(start, bot_container_name="oreeai-bot-x")
            wav = tmp_path / f"{call.id}.wav"
            if expected_status == CallStatus.done:
                storage.stage(call.id)
                wav.write_bytes(b"RIFF")
            await br.apply_exit_status(call.id, exit_code, result, cache, storage)
            fresh = await get_call(call.id)
            assert fresh.status == expected_status, (start, exit_code)
            if expected_status == CallStatus.done:
                assert fresh.end_reason == reason, (start, exit_code)
                assert fresh.transcript == [], (start, exit_code)
                assert fresh.audio_url == f"s3://test-bucket/calls/{call.id}/audio.wav", (
                    start,
                    exit_code,
                )
                assert storage.uploads == [call.id], (start, exit_code)
                assert not wav.exists(), (start, exit_code)  # scratch WAV deleted after upload
            else:
                assert fresh.failure_reason == reason, (start, exit_code)
                assert fresh.transcript is None, (start, exit_code)
                assert fresh.audio_url is None, (start, exit_code)
                assert storage.uploads == [], (start, exit_code)
            assert fresh.id in dispatched_calls, (start, exit_code)

    async def test_upload_failure_marks_failed(
        self,
        cache: CacheService,
        dispatched_calls: list[uuid.UUID],
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(CallStatus.recording, bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        wav = tmp_path / f"{call.id}.wav"
        wav.write_bytes(b"RIFF")
        storage.fail = True

        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache, storage)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "upload_failed"
        assert fresh.audio_url is None
        assert fresh.transcript is None
        assert wav.exists(), "scratch WAV kept as ops evidence on upload failure"
        assert fresh.id in dispatched_calls
        assert storage.uploads == []

    async def test_unguarded_storage_build_failure_marks_failed(
        self,
        cache: CacheService,
        dispatched_calls: list[uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """storage=None forces the lazy build; a broken configuration must
        degrade to upload_failed, never strand the call in processing."""
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(CallStatus.recording, bot_container_name="oreeai-bot-x")
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")

        def broken_build() -> Any:
            raise ConfigurationError("S3_BUCKET is not configured")

        monkeypatch.setattr(br, "build_object_storage_service", broken_build)
        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "upload_failed"
        assert fresh.audio_url is None
        assert fresh.id in dispatched_calls

    async def test_retention_fires_after_webhook(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(CallStatus.recording, bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")
        events: list[str] = []

        async def fake_dispatch(call_id: uuid.UUID) -> None:
            events.append("webhook")

        async def fake_retention() -> None:
            events.append("retention")

        monkeypatch.setattr(br, "dispatch_webhook", fake_dispatch)
        monkeypatch.setattr(br, "_run_retention_sweep", fake_retention)

        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache, storage)

        # The immediate sweep must run after the webhook so build_payload
        # still sees audio_url (the URI snapshot) before deletion.
        assert events == ["webhook", "retention"]

    async def test_missing_wav_marks_failed_upload(
        self,
        cache: CacheService,
        dispatched_calls: list[uuid.UUID],
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(CallStatus.recording, bot_container_name="oreeai-bot-x")

        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache, storage)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "upload_failed"
        assert fresh.audio_url is None
        assert fresh.id in dispatched_calls

    async def test_upload_failure_logs_never_carry_user_ref(
        self,
        cache: CacheService,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        call = await make_call(CallStatus.recording, bot_container_name="oreeai-bot-x")
        storage.stage(call.id)
        (tmp_path / f"{call.id}.wav").write_bytes(b"RIFF")
        storage.fail = True

        await br.apply_exit_status(call.id, 0, {"end_reason": "call_ended"}, cache, storage)

        assert str(call.id) in caplog.text
        assert "user_ref" not in caplog.text
        assert "test-user-1" not in caplog.text
        assert SECRET not in caplog.text

    async def test_clean_exit_from_joining_is_bot_error(self, cache: CacheService) -> None:
        call = await make_call(CallStatus.joining)
        await br.apply_exit_status(call.id, 0, None, cache)
        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "bot_error:exit_0"

    async def test_already_terminal_skips(
        self, cache: CacheService, dispatched_calls: list[uuid.UUID]
    ) -> None:
        call = await make_call(CallStatus.done)
        await br.apply_exit_status(call.id, 0, None, cache)
        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.done
        assert fresh.failure_reason is None
        assert fresh.id not in dispatched_calls


class TestPollOnce:
    async def test_claims_queued_call(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call = await make_call()

        async def fake_run_call(call_id: uuid.UUID, cache: CacheService) -> None:
            return None

        monkeypatch.setattr(br, "run_call", fake_run_call)
        monkeypatch.setattr(br, "_audio_disk_ok", lambda: True)

        assert await br.poll_once(cache) is True
        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.joining
        assert fresh.bot_container_name == f"oreeai-bot-{call.id}"

    async def test_no_queued_call_returns_false(self, cache: CacheService) -> None:
        assert await br.poll_once(cache) is False

    async def test_ceiling_race_fails_queued_call(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch, dispatched_calls: list
    ) -> None:
        await make_call(CallStatus.recording)
        queued = await make_call()
        monkeypatch.setattr(settings, "call_concurrency_limit", 1)

        assert await br.poll_once(cache) is True
        fresh = await get_call(queued.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "concurrency_limit"
        assert fresh.id in dispatched_calls

    async def test_disk_full_fails_queued_call(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch, dispatched_calls: list
    ) -> None:
        queued = await make_call()
        monkeypatch.setattr(br, "_audio_disk_ok", lambda: False)

        assert await br.poll_once(cache) is True
        fresh = await get_call(queued.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "disk_full"
        assert fresh.id in dispatched_calls


class TestSweeps:
    async def test_orphan_sweep_marks_non_terminal_and_kills(
        self,
        cache: CacheService,
        monkeypatch: pytest.MonkeyPatch,
        dispatched_calls: list[uuid.UUID],
    ) -> None:
        live = await make_call(CallStatus.recording)
        terminal = await make_call(CallStatus.done)

        docker_calls: list[tuple[str, ...]] = []

        async def fake_run_docker(*args: str) -> tuple[int, str, str]:
            docker_calls.append(args)
            if args[0] == "ps":
                return 0, f"oreeai-bot-{live.id}\noreeai-bot-{terminal.id}\nnot-a-call\n", ""
            return 0, "", ""

        monkeypatch.setattr(br, "run_docker", fake_run_docker)

        await br.orphan_sweep(cache)

        fresh = await get_call(live.id)
        assert fresh.status == CallStatus.failed
        assert fresh.failure_reason == "worker_restart"
        killed = [a[1] for a in docker_calls if a[0] == "kill"]
        assert f"oreeai-bot-{live.id}" in killed
        assert f"oreeai-bot-{terminal.id}" in killed
        assert "not-a-call" not in killed
        assert live.id in dispatched_calls
        assert terminal.id not in dispatched_calls

    async def test_stale_sweep_fails_old_calls_only(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch, dispatched_calls: list
    ) -> None:
        old_joining = await make_call(CallStatus.joining)
        fresh_recording = await make_call(CallStatus.recording)

        from datetime import UTC, datetime, timedelta

        old_ts = datetime.now(UTC) - timedelta(
            seconds=settings.bot_max_record_duration + br.STALE_CALL_GRACE_S + 60
        )
        async with runner_db_factory()() as session:
            await session.execute(
                update(Call).where(Call.id == old_joining.id).values(updated_at=old_ts)
            )
            await session.commit()

        docker_calls: list[tuple[str, ...]] = []

        async def fake_run_docker(*args: str) -> tuple[int, str, str]:
            docker_calls.append(args)
            return 0, "", ""

        monkeypatch.setattr(br, "run_docker", fake_run_docker)

        await br.stale_sweep(cache)

        stale = await get_call(old_joining.id)
        fresh = await get_call(fresh_recording.id)
        assert stale.status == CallStatus.failed
        assert stale.failure_reason == "stale_call"
        assert fresh.status == CallStatus.recording
        killed = [a[1] for a in docker_calls if a[0] == "kill"]
        assert f"oreeai-bot-{old_joining.id}" in killed
        assert not any(f"oreeai-bot-{fresh_recording.id}" in a for a in docker_calls)
        assert stale.id in dispatched_calls
        assert fresh.id not in dispatched_calls


class TestSpawnFlow:
    async def test_run_call_full_flow(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "s3_bucket", None)  # local storage fallback
        monkeypatch.setattr(settings, "environment", "local")
        call = await make_call(CallStatus.joining)
        profile_src = tmp_path / "profile"
        profile_src.mkdir()
        (profile_src / "Cookies").write_text("session")
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        wav = audio_dir / f"{call.id}.wav"
        wav.write_bytes(b"RIFF fake wav payload")
        monkeypatch.setattr(settings, "bot_profile", str(profile_src))
        monkeypatch.setattr(settings, "audio_host_path", str(audio_dir))
        seen_profile_dirs: list[str] = []

        async def fake_spawn_bot(
            args: Any, call_id: str, on_recording: Any = None
        ) -> tuple[int, dict[str, Any] | None]:
            profile_host = next(a.split(":")[0] for a in args if a.endswith(":/profile"))
            seen_profile_dirs.append(profile_host)
            assert Path(profile_host).is_dir(), "profile copy must exist during spawn"
            assert (Path(profile_host) / "Cookies").exists()
            if on_recording is not None:
                await on_recording()
            return 0, {"end_reason": "call_ended", "exit_code": 0}

        monkeypatch.setattr(br, "spawn_bot", fake_spawn_bot)

        await br.run_call(call.id, cache)

        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.done
        assert fresh.end_reason == "call_ended"
        assert seen_profile_dirs
        copy_dir = Path(seen_profile_dirs[0])
        assert copy_dir.parent == audio_dir
        assert copy_dir != profile_src
        assert not copy_dir.exists(), "profile copy removed after exit"
        # PR 6 seam: scratch WAV uploaded through the local fallback and
        # deleted; the stored object stays until retention reclaims it.
        stored = audio_dir / "objects" / f"calls/{call.id}/audio.wav"
        assert fresh.audio_url == f"file://{stored.resolve()}"
        assert not wav.exists(), "scratch WAV deleted after upload"
        assert stored.exists()

    async def test_run_call_skips_when_not_joinable(
        self, cache: CacheService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call = await make_call(CallStatus.failed)
        monkeypatch.setattr(settings, "bot_profile", "")

        async def boom(*args: Any) -> None:
            raise AssertionError("must not spawn")

        monkeypatch.setattr(br, "spawn_bot", boom)
        await br.run_call(call.id, cache)
        fresh = await get_call(call.id)
        assert fresh.status == CallStatus.failed


class TestStreamParsing:
    async def test_spawn_bot_parses_result_line(self) -> None:
        script = (
            "import sys;"
            "print('navigating');"
            "print('OREEAI_BOT_RESULT ' + "
            '\'{"call_id": "c", "end_reason": "alone", "exit_code": 0}\', flush=True)'
        )
        code, result = await br.spawn_bot(["python", "-c", script], "call-x")
        assert code == 0
        assert result is not None
        assert result["end_reason"] == "alone"

    async def test_spawn_bot_fires_recording_signal_once(self) -> None:
        script = (
            "print('oreeai bot starting');"
            "print('2026-09-10 | INFO | oreeai.bot.states | recording started', flush=True);"
            "print('recording started again (dedupe check)', flush=True)"
        )
        fired: list[int] = []

        async def on_recording() -> None:
            fired.append(1)

        code, _ = await br.spawn_bot(["python", "-c", script], "call-x", on_recording=on_recording)
        assert code == 0
        assert len(fired) == 1

    async def test_spawn_bot_missing_binary(self) -> None:
        code, result = await br.spawn_bot(["/nonexistent/binary-xyz"], "call-x")
        assert code == -1
        assert result is None

    def test_parse_result_line_variants(self) -> None:
        prefix = "2026-09-10T00:00:00 | INFO | oreeai.bot.result | "
        assert br.parse_result_line(prefix + 'OREEAI_BOT_RESULT {"exit_code": 0}') == {
            "exit_code": 0
        }
        assert br.parse_result_line("no result here") is None
        assert br.parse_result_line("OREEAI_BOT_RESULT {not json") is None


class TestAudioDiskCheck:
    def test_missing_audio_dir_fails_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "audio_host_path", "/nonexistent/volume/path")
        assert br._audio_disk_ok() is False

    def test_disk_floor(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        assert br._audio_disk_ok() is True


class TestImportBoundary:
    def test_workers_never_import_bot(self) -> None:
        root = Path("src/oreeai_notetaker")
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    # The boundary forbids the `bot` package itself — not
                    # botocore/boto3, which the storage adapters need.
                    assert all(
                        a.name != "bot" and not a.name.startswith("bot.") for a in node.names
                    ), path
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module != "bot" and not node.module.startswith("bot."), path

    def test_no_docker_socket_reference_in_package(self) -> None:
        for path in Path("src/oreeai_notetaker").rglob("*.py"):
            assert "docker.sock" not in path.read_text(), path


def test_active_statuses_match_contract() -> None:
    assert (
        CallStatus.queued,
        CallStatus.joining,
        CallStatus.recording,
        CallStatus.processing,
    ) == ACTIVE_STATUSES
