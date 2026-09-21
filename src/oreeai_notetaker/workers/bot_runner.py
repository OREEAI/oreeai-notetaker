"""DB-driven bot runner — the standalone orchestration process (PR 5).

Long-running poller invoked as::

    python -m oreeai_notetaker.workers.bot_runner

Owns the orchestration contract (README.md, Call lifecycle):
polls ``Call`` rows (2 s), claims work with
``SELECT ... FOR UPDATE SKIP LOCKED`` so a second runner instance is
harmless (exactly one runner is the supported configuration), re-checks
the concurrency ceiling and free disk race-free against the API, spawns
the bot container with the PR 4 resource envelope (``--rm``, never a
restart flag — the streaming wait owns the exit), streams bot output
tagged with ``call_id``, maps bot exit codes through the table in
bot/README.md,
uploads the scratch WAV to object storage, transcribes it (PR 7 —
the ``processing -> done`` transition is driven by the real transcript
landing; the honest-empty ``[]`` is legitimate for a muted call),
heartbeats Redis every 10 s, sweeps orphans on
startup, stale calls every 60 s (``joining``/``recording`` AND
``processing`` — PR 7 extends the sweep to cover hung transcription),
and expired audio every 60 s
(retention worker, PR 6 — once immediately after each done as well),
and fires the signed webhook on every terminal transition.

The API process never gets the docker socket; only this runner (and the
containers it spawns) touches docker. Logs carry call ids and container
names only — never audio bytes, never recording paths paired with
``user_ref``, never ``webhook_secret``, never the S3 object key in the
same line as a ``user_ref`` (the storage layer logs none of these at
all).
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import or_, select

from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.core.exceptions import ConflictError
from oreeai_notetaker.core.logging import setup_logging
from oreeai_notetaker.db.session import session_factory
from oreeai_notetaker.enums import TERMINAL_STATUSES, CallStatus
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
    ObjectStorageError,
    SourceUnavailable,
    UploadFailed,
)
from oreeai_notetaker.integrations.transcription.base import (
    Transcript,
    TranscriptionError,
)
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.repositories.call import CallRepository
from oreeai_notetaker.services.call import CallService
from oreeai_notetaker.services.storage import (
    ObjectStorageService,
    build_object_storage_service,
)
from oreeai_notetaker.services.transcription import (
    TranscriptionService,
    build_transcription_service,
)
from oreeai_notetaker.workers.retention import (
    RETENTION_INTERVAL_S,
    enforce_retention,
)
from oreeai_notetaker.workers.webhook_dispatcher import deliver

logger = logging.getLogger("oreeai.runner")

CONTAINER_PREFIX = "oreeai-bot-"

POLL_INTERVAL_S = 2.0
HEARTBEAT_INTERVAL_S = 10.0
STALE_SWEEP_INTERVAL_S = 60.0
STALE_CALL_GRACE_S = 300
# Stale `processing` cutoff (PR 7): transcription worst case is 3×660 s
# provider read-timeout + 1/4 s sleeps + upload time ≈ 2090 s; 2400 s
# leaves slack (arithmetic is S3-transport-specific — the provider
# fetches, so each attempt is bounded by the 660 s read timeout; the
# dev binary-body transport streams up to 2 GB from this host per
# attempt and can legitimately exceed 2400 s, where stale-fail is an
# honest, benign outcome with the audio riding the failed window). A
# runner crash during transcription would otherwise strand the call in
# `processing` forever (the stale sweep is the only watchdog for it —
# the bot container is gone by then).
PROCESSING_STALE_CUTOFF_S = 2400
DISK_MIN_FREE_BYTES = 2 * 1024**3

# Resource envelope — mirrors bot/docker-compose.yml (parity is locked by
# tests so the two artifacts cannot drift). No --restart: the engine
# rejects it together with --rm, and the runner waits on the exit itself.
MEM_LIMIT = "2g"
CPUS = "1.5"
PIDS_LIMIT = "512"
LOG_MAX_SIZE = "10m"
LOG_MAX_FILE = "3"

# Bot-facing ambient variables forwarded when set in the runner's own
# environment (mirrors the PR 4 passthrough allowlist). MEETING_URL,
# CALL_ID and CONSENT_ACK are always set by the runner, never passthrough.
_ENV_PASSTHROUGH: tuple[str, ...] = (
    "BOT_AUTH_MODE",
    "BOT_PROFILE_DIR",
    "ENVIRONMENT",
    "LOG_LEVEL",
    "TZ",
    "PAREC_DEVICE",
    "BOT_WAITING_ROOM_TIMEOUT",
    "BOT_EMPTY_ROOM_TIMEOUT",
    "BOT_ALONE_GRACE",
    "BOT_MAX_RECORD_DURATION",
    "BOT_SILENCE_RMS_FLOOR",
)

_RESULT_LINE_RE = re.compile(r"OREEAI_BOT_RESULT (\{.*\})")
_RECORDING_SIGNAL_RE = re.compile(r"\brecording started\b")
_VALID_END_REASONS = frozenset({"call_ended", "removed", "alone", "give_up"})

ExitCode = int | None
DockerResult = tuple[int, str, str]

_RUNNING_TASKS: set[asyncio.Task[None]] = set()


def container_name(call_id: uuid.UUID) -> str:
    return f"{CONTAINER_PREFIX}{call_id}"


def parse_result_line(line: str) -> dict[str, Any] | None:
    match = _RESULT_LINE_RE.search(line)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _result_end_reason(result: dict[str, Any] | None) -> str | None:
    if result is None:
        return None
    value = result.get("end_reason")
    return value if isinstance(value, str) and value in _VALID_END_REASONS else None


def build_spawn_args(
    call: Call,
    profile_host_path: str | None = None,
    *,
    image: str | None = None,
    command: Sequence[str] | None = None,
) -> list[str]:
    """Full ``docker run`` argv for one bot, envelope flags included.

    ``--rm`` is kept (the runner waits on the exit itself) and no
    ``--restart`` flag is ever emitted — the docker engine rejects the
    combination and bounded retries would fight the streaming wait (see
    the PR 4 handoff notes). ``image``/``command`` exist for the
    docker-marked tests, which run tiny stand-in containers instead of
    the bot image.
    """
    args = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--shm-size=1g",
        f"--name={container_name(call.id)}",
        f"--memory={MEM_LIMIT}",
        f"--cpus={CPUS}",
        f"--pids-limit={PIDS_LIMIT}",
        "--log-driver",
        "json-file",
        "--log-opt",
        f"max-size={LOG_MAX_SIZE}",
        "--log-opt",
        f"max-file={LOG_MAX_FILE}",
        "--network",
        settings.bot_docker_network,
    ]
    env_values: dict[str, str] = {
        "MEETING_URL": call.meeting_url,
        "CALL_ID": str(call.id),
        "CONSENT_ACK": "true",
    }
    for key in _ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            env_values[key] = value
    if profile_host_path:
        env_values["BOT_PROFILE_DIR"] = "/profile"
    for key, value in env_values.items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{settings.audio_host_path}:/audio"]
    if profile_host_path:
        args += ["-v", f"{profile_host_path}:/profile"]
    args.append(image or f"oreeai-bot:{settings.bot_image_tag}")
    if command:
        args += list(command)
    return args


async def run_docker(*args: str) -> DockerResult:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else -1
    return returncode, stdout.decode(), stderr.decode()


async def _stream_output(
    call_id: str,
    proc: asyncio.subprocess.Process,
    on_recording: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any] | None:
    """Stream bot stdout/stderr into the runner log, tagged; return the
    parsed ``OREEAI_BOT_RESULT`` payload when one appears. ``on_recording``
    fires once on the bot's ``recording started`` line — the ``joining ->
    recording`` transition driver."""
    result: dict[str, Any] | None = None
    recording_fired = False

    async def pump(stream: asyncio.StreamReader | None) -> None:
        nonlocal result, recording_fired
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            logger.info("[%s] bot: %s", call_id, line)
            parsed = parse_result_line(line)
            if parsed is not None:
                result = parsed
            fire = on_recording
            if fire is not None and not recording_fired and _RECORDING_SIGNAL_RE.search(line):
                recording_fired = True
                await fire()

    await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
    return result


async def spawn_bot(
    args: Sequence[str],
    call_id: str,
    on_recording: Callable[[], Awaitable[None]] | None = None,
) -> tuple[ExitCode, dict[str, Any] | None]:
    """Run the bot container argv and wait for its exit, streaming output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.error("[%s] bot spawn failed: %s", call_id, exc)
        return -1, None
    result = await _stream_output(call_id, proc, on_recording)
    code = await proc.wait()
    return code, result


def _failure_reason(exit_code: ExitCode, status: CallStatus) -> str:
    if exit_code == 2:
        return "never_admitted"
    if exit_code == 4:
        return "no_show" if status == CallStatus.recording else "join_timeout"
    if exit_code == 6:
        return "consent_missing"
    if exit_code == 7:
        return "silent_recording"
    return f"bot_error:exit_{exit_code}"


async def dispatch_webhook(call_id: uuid.UUID) -> None:
    async with session_factory() as session:
        call = await CallRepository(session).get(call_id)
        if call is None or call.webhook_delivered_at is not None:
            return
        if call.status not in TERMINAL_STATUSES:
            return
        try:
            await deliver(call, session)
        except Exception:
            logger.exception("webhook dispatch failed for call %s", call_id)
        await session.commit()


async def apply_exit_status(
    call_id: uuid.UUID,
    exit_code: ExitCode,
    result: dict[str, Any] | None,
    cache: CacheService,
    storage: ObjectStorageService | None = None,
    transcription: TranscriptionService | None = None,
) -> None:
    """Map a bot exit through the table in bot/README.md.

    Clean exits (0, and 3 = removed mid-call) and the disambiguated
    exit-7-``alone`` case run the PR 6 storage seam followed by the PR 7
    transcription seam: ``recording -> processing`` commits first (row
    lock released before network I/O), then the scratch WAV is uploaded,
    then the stored object is made transcribable (``AudioSource`` — the
    storage adapter owns the transport), then the provider call runs
    with the service retry policy, then ``processing -> done`` lands
    atomically with ``audio_url`` AND the real transcript segments, then
    the local WAV is deleted, then the webhook fires. On any permanent
    failure after the upload the call is ``failed/
    transcription_failed:<detail>`` (or ``failed/upload_failed`` before
    the upload) and done is never reached; the stored object then rides
    the failed-audio retention window.
    """
    async with session_factory() as session:
        repo = CallRepository(session)
        service = CallService(repo, cache)
        call = await repo.get(call_id)
        if call is None or call.status in TERMINAL_STATUSES:
            return
        clean = exit_code in (0, 3) and call.status == CallStatus.recording
        alone_via_silence = (
            exit_code == 7
            and call.status == CallStatus.recording
            and _result_end_reason(result) == "alone"
        )
        try:
            if clean or alone_via_silence:
                if alone_via_silence:
                    end_reason = "alone"
                elif exit_code == 3:
                    end_reason = "removed"
                else:
                    end_reason = _result_end_reason(result) or "call_ended"
                await service.mark_processing(call_id)
            else:
                reason = _failure_reason(exit_code, call.status)
                await service.mark_failed(call_id, reason)
                logger.info("call %s failed (bot exit %s, reason=%s)", call_id, exit_code, reason)
        except ConflictError:
            await session.rollback()
            return
        await session.commit()

    if not (clean or alone_via_silence):
        await dispatch_webhook(call_id)
        return

    audio_url = await _upload_call_audio(call_id, cache, storage)
    if audio_url is None:
        return  # upload_failed path: already marked failed + webhooked
    source = await _transcribable_source(call_id, cache, audio_url, storage)
    if source is None:
        return  # source_unavailable path: already marked failed + webhooked
    transcript = await _transcribe_call(call_id, cache, audio_url, source, transcription)
    if transcript is None:
        return  # transcription_failed path: already marked failed + webhooked
    await _finish_done_call(call_id, end_reason, audio_url, cache, transcript)


async def _transcribable_source(
    call_id: uuid.UUID,
    cache: CacheService,
    audio_url: str,
    storage: ObjectStorageService | None = None,
) -> AudioSource | None:
    """Make the stored object transcribable (transport owned by the
    storage adapter: S3 → presigned GET; local → stored-copy path).

    ``SourceUnavailable`` means the stored object vanished or is
    unreadable after a successful upload — an operational anomaly. The
    call is marked ``failed/transcription_failed:source_unavailable``
    WITH the ``audio_url`` snapshot (the failed-audio retention window
    reclaims the object if it reappears or for audit); no source is
    returned when the failure is already handled.
    """
    if storage is None:
        try:
            storage = build_object_storage_service()
        except ConfigurationError as exc:
            logger.error("call %s transcription failed: storage misconfigured: %s", call_id, exc)
            await _mark_transcription_failed(call_id, cache, "storage_misconfigured", audio_url)
            return None
    try:
        return await storage.transcribable_source_for_call(call_id)
    except (SourceUnavailable, ObjectStorageError) as exc:
        logger.error("call %s transcription failed: source unavailable: %s", call_id, exc)
        await _mark_transcription_failed(call_id, cache, "source_unavailable", audio_url)
        return None


async def _transcribe_call(
    call_id: uuid.UUID,
    cache: CacheService,
    audio_url: str,
    source: AudioSource,
    transcription: TranscriptionService | None = None,
) -> Transcript | None:
    """Run transcription under the service's retry policy.

    Permanent outcomes — including ``AudioTooLarge`` (the size guard)
    and transient-exhaustion (re-raised permanent after 3 attempts) —
    mark the call ``failed/transcription_failed:<detail>`` and persist
    the ``audio_url`` snapshot so the now-live failed-audio retention
    window (``FAILED_AUDIO_RETENTION_DAYS``, default 7) owns the stored
    object: the runner deliberately never deletes it here, the periodic
    retention tick reclaims it at the cutoff. Returns ``None`` after
    marking + webhook.
    """
    if transcription is None:
        try:
            transcription = build_transcription_service()
        except ConfigurationError as exc:
            logger.error("call %s transcription failed: misconfigured: %s", call_id, exc)
            await _mark_transcription_failed(call_id, cache, f"misconfigured: {exc}", audio_url)
            return None
    try:
        return await transcription.transcribe(source)
    except TranscriptionError as exc:
        detail = _failure_detail(exc)
        logger.error("call %s transcription failed: %s", call_id, detail)
        await _mark_transcription_failed(call_id, cache, detail, audio_url)
        return None


async def _mark_transcription_failed(
    call_id: uuid.UUID,
    cache: CacheService,
    detail: str,
    audio_url: str,
) -> None:
    """Mark failed with the transcription reason AND the stored-object
    snapshot — the failed-audio retention window keys off
    ``status=failed`` + ``audio_url IS NOT NULL``, so the object must be
    persisted here or the sweep would never reclaim it."""
    async with session_factory() as session:
        repo = CallRepository(session)
        service = CallService(repo, cache)
        try:
            await service.mark_failed(call_id, f"transcription_failed:{detail}")
            call = await repo.get(call_id)
            if call is not None and audio_url:
                await repo.update(call, {"audio_url": audio_url})
            await session.commit()
        except ConflictError:
            await session.rollback()
            return
    # The upload already succeeded, so the stored copy (now persisted on
    # the row and owned by the failed-audio retention window) is the
    # durable record — the scratch WAV is redundant disk residue and is
    # deleted here exactly as it is on the done path (best-effort).
    _delete_local_wav(call_id)
    await dispatch_webhook(call_id)


def _failure_detail(exc: Exception) -> str:
    """Exception message, sanitized for ``failure_reason`` (counts and
    status codes only by construction; no transcript text, no paths)."""
    return str(exc)


async def _upload_call_audio(
    call_id: uuid.UUID,
    cache: CacheService,
    storage: ObjectStorageService | None = None,
) -> str | None:
    """Upload the scratch WAV (``AUDIO_HOST_PATH/<call_id>.wav``) to storage.

    Returns the object URI, or ``None`` when the upload did not land:
    the local WAV is missing (disk died, container crashed) or the
    adapter raised ``UploadFailed``. Either way the call is marked
    ``failed/upload_failed`` and webhooked — no re-record loop; the
    failed-audio retention window owns any storage-side residue. The
    raised error message is call-id-only (never the object key).
    """
    wav_path = Path(settings.audio_host_path) / f"{call_id}.wav"
    if not wav_path.exists():
        logger.error("call %s upload failed: local WAV missing", call_id)
        await _mark_upload_failed(call_id, cache)
        return None
    if storage is None:
        # main() pre-builds the service (fail-fast), so this only triggers
        # on unguarded call paths (tests, future queue workers) — degrade
        # to upload_failed instead of stranding the call in processing.
        try:
            storage = build_object_storage_service()
        except ConfigurationError as exc:
            logger.error("call %s upload failed: storage misconfigured: %s", call_id, exc)
            await _mark_upload_failed(call_id, cache)
            return None
    try:
        audio_url = await storage.upload_for_call(call_id, wav_path)
    except UploadFailed as exc:
        logger.error("call %s upload failed: %s", call_id, exc)
        await _mark_upload_failed(call_id, cache)
        return None
    logger.info("call %s uploaded to object storage", call_id)
    return audio_url


async def _mark_upload_failed(call_id: uuid.UUID, cache: CacheService) -> None:
    async with session_factory() as session:
        service = CallService(CallRepository(session), cache)
        try:
            await service.mark_failed(call_id, "upload_failed")
            await session.commit()
        except ConflictError:
            await session.rollback()
            return
    await dispatch_webhook(call_id)


async def _finish_done_call(
    call_id: uuid.UUID,
    end_reason: str,
    audio_url: str,
    cache: CacheService,
    transcript: Transcript,
) -> None:
    """Land ``processing -> done`` with the object URI and real segments,
    then scratch cleanup.

    ``audio_url`` and the transcript are written atomically with the
    ``done`` transition (segments round-trip as plain dicts — the JSONB
    column is a storage concern, never Postgres-specific operators);
    the local WAV is deleted only afterwards — a crash before ``done``
    would otherwise strand the call in ``processing`` (the PR 7 stale
    sweep now covers that), while a crash after ``done`` leaves a benign
    orphan WAV (scratch-space policy, documented in the README's Data
    retention section).
    """
    segments = [segment.model_dump() for segment in transcript.segments]
    async with session_factory() as session:
        service = CallService(CallRepository(session), cache)
        try:
            await service.mark_done(call_id, end_reason, transcript=segments, audio_url=audio_url)
            await session.commit()
        except ConflictError:
            await session.rollback()
            return
    logger.info("call %s done (end_reason=%s, segments=%s)", call_id, end_reason, len(segments))
    _delete_local_wav(call_id)
    await dispatch_webhook(call_id)
    # Immediate retention (PR 6): fires AFTER the webhook, so build_payload
    # still saw audio_url and delivered the URI snapshot even when
    # AUDIO_RETENTION_DAYS=0 deletes the object right away. The sweep is
    # never-crash (per-row try/except inside), the guard is belt-and-braces.
    await _run_retention_sweep()


async def _run_retention_sweep() -> None:
    """Best-effort retention call site — never crashes the runner."""
    try:
        await enforce_retention()
    except Exception:
        logger.exception("retention sweep failed; continuing")


def _delete_local_wav(call_id: uuid.UUID) -> None:
    """Remove the scratch WAV once the object is safely stored (best-effort)."""
    path = Path(settings.audio_host_path) / f"{call_id}.wav"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("local WAV cleanup failed for call %s: %s", call_id, exc)


def _copy_profile(call_id: uuid.UUID) -> str | None:
    """Per-call copy of the persistent Chrome profile (Chrome's
    SingletonLock forbids two live browsers on one profile dir). The copy
    lands under the audio host path, which is a real host path the spawned
    bot can mount even when the runner itself runs containerized; the
    original profile dir is never locked by a browser."""
    source = settings.bot_profile
    if not source or not Path(source).exists():
        return None
    target = Path(settings.audio_host_path) / f".profile-{call_id}"
    try:
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=False)
    except OSError:
        logger.warning("profile copy failed for call %s; spawning without profile", call_id)
        return None
    return str(target)


def _discard_profile(profile_dir: str | None) -> None:
    if profile_dir is None:
        return
    shutil.rmtree(profile_dir, ignore_errors=True)


async def _mark_recording(call_id: uuid.UUID, cache: CacheService) -> None:
    async with session_factory() as session:
        service = CallService(CallRepository(session), cache)
        try:
            await service.mark_recording(call_id)
            await session.commit()
            logger.info("call %s recording", call_id)
        except ConflictError:
            await session.rollback()


async def run_call(call_id: uuid.UUID, cache: CacheService) -> None:
    profile_dir = _copy_profile(call_id)
    try:
        async with session_factory() as session:
            call = await CallRepository(session).get(call_id)
            if call is None or call.status != CallStatus.joining:
                logger.info("call %s no longer joinable at spawn time; skipping", call_id)
                return
            spawn_args = build_spawn_args(call, profile_dir)
        logger.info(
            "spawning %s (image oreeai-bot:%s, network %s)",
            container_name(call_id),
            settings.bot_image_tag,
            settings.bot_docker_network,
        )
        exit_code, result = await spawn_bot(
            spawn_args, str(call_id), on_recording=lambda: _mark_recording(call_id, cache)
        )
    finally:
        _discard_profile(profile_dir)
    await apply_exit_status(call_id, exit_code, result, cache)


def _audio_disk_ok() -> bool:
    try:
        return shutil.disk_usage(settings.audio_host_path).free >= DISK_MIN_FREE_BYTES
    except OSError:
        return False


async def poll_once(cache: CacheService) -> bool:
    """Claim at most one queued call. Returns True when one was claimed.

    The claim holds the row lock while the ceiling and disk checks run,
    so two runners (or a runner and the API) cannot both admit past the
    ceiling; the API's own check remains the queue-intake gate.
    """
    claimed_call_id: uuid.UUID | None = None
    failed_call_id: uuid.UUID | None = None
    async with session_factory() as session:
        repo = CallRepository(session)
        service = CallService(repo, cache)
        stmt = (
            select(Call)
            .where(Call.status == CallStatus.queued)
            .order_by(Call.created_at, Call.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        call = (await session.execute(stmt)).scalars().first()
        if call is None:
            return False
        call_id = call.id

        active = await repo.count_active()
        if active > settings.call_concurrency_limit:
            await service.mark_failed(call_id, "concurrency_limit")
            await session.commit()
            logger.info("call %s refused: concurrency limit reached (%s active)", call_id, active)
            failed_call_id = call_id
        elif not _audio_disk_ok():
            await service.mark_failed(call_id, "disk_full")
            await session.commit()
            logger.warning("call %s refused: audio volume below free-disk floor", call_id)
            failed_call_id = call_id
        else:
            await service.mark_bot_started(call_id, container_name(call_id))
            await session.commit()
            logger.info("call %s claimed, moving to joining", call_id)
            claimed_call_id = call_id

    if failed_call_id is not None:
        await dispatch_webhook(failed_call_id)
    if claimed_call_id is not None:
        task = asyncio.create_task(run_call(claimed_call_id, cache))
        _RUNNING_TASKS.add(task)
        task.add_done_callback(_RUNNING_TASKS.discard)
    return True


async def orphan_sweep(cache: CacheService) -> None:
    """Startup sweep: containers named ``oreeai-bot-*`` whose call is
    terminal or missing are killed and removed; non-terminal calls are
    marked ``failed/worker_restart`` (this runner instance cannot reattach
    to a browser the previous runner spawned)."""
    code, stdout, _ = await run_docker(
        "ps", "-a", "--filter", f"name={CONTAINER_PREFIX}", "--format", "{{.Names}}"
    )
    if code != 0:
        logger.warning("orphan sweep could not list containers; skipping")
        return
    for name in stdout.splitlines():
        name = name.strip()
        if not name.startswith(CONTAINER_PREFIX):
            continue
        raw = name[len(CONTAINER_PREFIX) :]
        try:
            call_id = uuid.UUID(raw)
        except ValueError:
            logger.warning("orphan sweep found non-call container %s; removing", name)
            await run_docker("rm", "-f", name)
            continue

        restart_failed = False
        async with session_factory() as session:
            repo = CallRepository(session)
            call = await repo.get(call_id)
            if call is not None and call.status not in TERMINAL_STATUSES:
                try:
                    service = CallService(repo, cache)
                    await service.mark_failed(call_id, "worker_restart")
                    await session.commit()
                    restart_failed = True
                except ConflictError:
                    await session.rollback()
        if restart_failed:
            logger.warning("orphan sweep marked call %s failed (worker_restart)", call_id)
        await run_docker("kill", name)
        await run_docker("rm", "-f", name)
        if restart_failed:
            await dispatch_webhook(call_id)


async def stale_sweep(cache: CacheService) -> None:
    """Every 60 s: calls in joining/recording whose ``updated_at`` is
    older than ``BOT_MAX_RECORD_DURATION + STALE_CALL_GRACE_S`` are
    killed and marked ``failed/stale_call``; and (PR 7) calls in
    ``processing`` past ``PROCESSING_STALE_CUTOFF_S`` likewise — a
    hung transcription (or a runner crash mid-transcription) otherwise
    strands the call in ``processing`` forever, since the bot container
    is already gone by that stage.

    Never crashes the runner: the sweep is now the sole watchdog for
    hung processing calls, so a transient DB error or a missing docker
    binary must be logged and skipped, not propagated into the main
    loop (mirrors ``_run_retention_sweep``).
    """
    try:
        await _stale_sweep_inner(cache)
    except Exception:
        logger.exception("stale sweep failed; continuing")


async def _stale_sweep_inner(cache: CacheService) -> None:
    now = datetime.now(UTC)
    recording_cutoff = now - timedelta(
        seconds=settings.bot_max_record_duration + STALE_CALL_GRACE_S
    )
    processing_cutoff = now - timedelta(seconds=PROCESSING_STALE_CUTOFF_S)
    async with session_factory() as session:
        stmt = select(Call.id).where(
            or_(
                Call.status.in_((CallStatus.joining, CallStatus.recording))
                & (Call.updated_at < recording_cutoff),
                (Call.status == CallStatus.processing) & (Call.updated_at < processing_cutoff),
            )
        )
        stale_ids = (await session.execute(stmt)).scalars().all()

    for call_id in stale_ids:
        logger.warning("stale sweep: call %s past stale cutoff; failing", call_id)
        # Best-effort container kill: joining/recording bots are usually
        # alive; a processing call's container is long gone (--rm), where
        # this is a harmless no-op against any residue.
        await run_docker("kill", container_name(call_id))
        await run_docker("rm", "-f", container_name(call_id))
        async with session_factory() as session:
            service = CallService(CallRepository(session), cache)
            try:
                await service.mark_failed(call_id, "stale_call")
                await session.commit()
            except ConflictError:
                await session.rollback()
                continue
        await dispatch_webhook(call_id)


async def _heartbeat(cache: CacheService) -> None:
    now_iso = datetime.now(UTC).isoformat()
    await cache.set(cache.key("runner", "heartbeat"), now_iso, ttl=60)


def _prepare_audio_dir() -> bool:
    path = Path(settings.audio_host_path)
    with contextlib.suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return _audio_disk_ok()


async def main() -> None:
    setup_logging(settings.log_level)

    # Storage and transcription selection happen first and fail-fast: a
    # runner that cannot store audio or transcribe it must not run
    # (ConfigurationError in production with unset vars; local/stub
    # stand-ins elsewhere, which warn loudly).
    build_object_storage_service()
    build_transcription_service()

    cache = CacheService(None)
    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        cache = CacheService(client)
    except (RedisError, OSError):
        logger.warning("redis unavailable; runner heartbeats will be no-ops")

    if not _prepare_audio_dir():
        logger.warning(
            "audio volume %s not ready or below %s bytes free; spawns will fail disk_full",
            settings.audio_host_path,
            DISK_MIN_FREE_BYTES,
        )

    logger.info(
        "bot runner starting: image=oreeai-bot:%s audio=%s ceiling=%s",
        settings.bot_image_tag,
        settings.audio_host_path,
        settings.call_concurrency_limit,
    )
    await orphan_sweep(cache)

    next_heartbeat = 0.0
    next_stale_sweep = 0.0
    next_retention = 0.0
    try:
        while True:
            now = asyncio.get_running_loop().time()
            if now >= next_heartbeat:
                next_heartbeat = now + HEARTBEAT_INTERVAL_S
                await _heartbeat(cache)
            if now >= next_stale_sweep:
                next_stale_sweep = now + STALE_SWEEP_INTERVAL_S
                await stale_sweep(cache)
            if now >= next_retention:
                next_retention = now + RETENTION_INTERVAL_S
                await _run_retention_sweep()
            await poll_once(cache)
            await asyncio.sleep(POLL_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("bot runner stopping; active bots keep running (orphan sweep reaps them)")
        raise


if __name__ == "__main__":
    asyncio.run(main())
