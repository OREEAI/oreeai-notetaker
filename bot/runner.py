"""Precursor bot-runner orchestrator with a hard concurrency ceiling (PR 4).

PRECURSOR — DO NOT EXTEND: this file is deliberately temporary. PR 5
replaces it with the DB-driven runner (`workers/bot_runner.py` inside the
`oreeai_notetaker` package), which polls `Call` rows instead of a CLI/file
queue, maps bot exit codes through the shared-contracts table, and does
active-count bookkeeping in Postgres instead of a lockfile. If behavior is
needed from this script, port it there — do not grow this file.

What this is: a standalone loop that spawns bot containers via `docker run`
(detached), enforces the concurrency ceiling (default 3,
`CALL_CONCURRENCY_LIMIT`), and tracks active call ids in a JSON lockfile.
On startup — and on every poll tick — a reconcile pass reaps lockfile
entries whose containers have exited (crashed, OOM-killed) or vanished
(operator cleanup, or the runner itself was SIGKILLed with no chance to
unregister), removing the finished containers as it goes.

Consent: the runner passes `CONSENT_ACK=true` to every bot it spawns. That
is an *operator-level* attestation — the human running the runner has
acknowledged recording consent for the calls they queue. The API-level gate
(`consent_ack` on `POST /calls`) lands in PR 5; exit code 6 /
`consent_missing` keep the exact meaning defined in the shared contracts.

Exactly one runner instance may run: an flock hold on `<lock>.hold`
refuses a second start (the ceiling bookkeeping is per-instance).

Independence (standing rules): standard library only — this module must
never import `oreeai_notetaker`, and the docker socket lives here (the
runner), never in the API process. Logs carry call ids and container names
only: never audio bytes, never recording paths paired with identifiers.

Usage:
    uv run python -m bot.runner                      # interactive (join/status/quit)
    uv run python -m bot.runner --queue-file X.jsonl  # also drain a JSONL queue
    make bot-runner                                   # via Makefile (.env-loaded, N=3)

Queue-file request schema (one JSON object per line):
    {"meeting_url": "https://meet.google.com/xxx-xxxx-xxx",
     "call_id":  "optional, generated when absent",
     "env":      {"BOT_AUTH_MODE": "anonymous"},   # overrides ambient env
     "profile":  "/path/to/chrome-profile",        # per-container /profile mount
     "command":  ["sleep", "600"]}                  # container cmd; tests only
Precedence for every passthrough variable: request `env` > runner ambient env
(the Makefile further layers command line > .env > shell env before the
runner starts). Refused or failed requests are appended to `<queue>.rejected`
so nothing is silently dropped.

The resource envelope below is pinned to mirror `bot/docker-compose.yml`;
`tests/bot/test_runner.py` asserts both artifacts carry the same numbers so
they cannot drift apart.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("oreeai.runner")

CONTAINER_PREFIX = "oreeai-bot-"

DEFAULT_CONCURRENCY = 3
DEFAULT_IMAGE_TAG = "local"
DEFAULT_AUDIO_HOST_PATH = "/var/lib/oreeai/audio"
DEFAULT_LOCK_PATH = "/var/lib/oreeai/runner.lock"
POLL_INTERVAL_S = 2.0

# Resource envelope — the docker-run spelling of bot/docker-compose.yml.
# Pinned by tests in both directions; change the compose file and the test
# suite fails loudly. pids_limit 512: Chromium + pulse + xvfb measured ~40
# processes in-container, so 512 is comfortable headroom (PR 4 chunk; if a
# real run ever proves otherwise, raise to 1024 and note it in the Handoff
# Notes — do not raise silently).
MEM_LIMIT = "2g"
CPUS = "1.5"
PIDS_LIMIT = "512"
RESTART_POLICY = "on-failure:2"
LOG_MAX_SIZE = "10m"
LOG_MAX_FILE = "3"

# Bot-facing ambient variables forwarded to every container when set
# (mirrors the Makefile's `-e` allowlist). MEETING_URL / CALL_ID /
# CONSENT_ACK are always set by the runner itself, never passthrough.
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

DockerCommand = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class SpawnError(RuntimeError):
    """Raised when a bot container cannot be spawned."""


def container_name(call_id: str) -> str:
    return f"{CONTAINER_PREFIX}{call_id}"


@dataclass(frozen=True)
class RunnerConfig:
    image: str = f"oreeai-bot:{DEFAULT_IMAGE_TAG}"
    concurrency: int = DEFAULT_CONCURRENCY
    audio_host_path: str = DEFAULT_AUDIO_HOST_PATH
    lock_path: str = DEFAULT_LOCK_PATH
    queue_file: str | None = None
    debug_host_path: str | None = None
    profile_host_path: str | None = None
    env: Mapping[str, str] | None = None  # ambient; None means os.environ

    @property
    def ambient_env(self) -> Mapping[str, str]:
        return os.environ if self.env is None else self.env

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        concurrency: int | None = None,
        image: str | None = None,
        queue_file: str | None = None,
    ) -> RunnerConfig:
        limit = concurrency
        if limit is None:
            raw = environ.get("CALL_CONCURRENCY_LIMIT", "").strip()
            if raw.isdigit():
                limit = int(raw)
            elif raw:
                logger.warning(
                    "ignoring invalid CALL_CONCURRENCY_LIMIT=%r, using %d",
                    raw,
                    DEFAULT_CONCURRENCY,
                )
                limit = DEFAULT_CONCURRENCY
            else:
                limit = DEFAULT_CONCURRENCY
        tag = environ.get("BOT_IMAGE_TAG", "").strip() or DEFAULT_IMAGE_TAG
        return cls(
            image=image or f"oreeai-bot:{tag}",
            concurrency=limit,
            audio_host_path=environ.get("AUDIO_HOST_PATH", "").strip() or DEFAULT_AUDIO_HOST_PATH,
            lock_path=environ.get("RUNNER_LOCK_PATH", "").strip() or DEFAULT_LOCK_PATH,
            queue_file=queue_file
            if queue_file is not None
            else (environ.get("RUNNER_QUEUE_FILE", "").strip() or None),
            debug_host_path=environ.get("DEBUG_HOST_PATH", "").strip() or None,
            profile_host_path=environ.get("BOT_PROFILE", "").strip() or None,
            env=environ,
        )


@dataclass(frozen=True)
class SpawnRequest:
    meeting_url: str
    call_id: str
    env: Mapping[str, str] = field(default_factory=dict)
    profile_host_path: str | None = None
    debug_host_path: str | None = None
    command: tuple[str, ...] | None = None
    raw: str = ""


# ---------------------------------------------------------------------------
# pure pieces: admission, lockfile, argv building, reconcile planning
# ---------------------------------------------------------------------------


def admit(active: Sequence[str], limit: int, call_id: str) -> str | None:
    """Return None when the request fits under the ceiling, else the refusal.

    Also refuses duplicate call ids already tracked as active. Clear message
    naming the limit per the PR 4 chunk ("concurrency limit reached").
    """
    if limit < 1:
        return f"invalid concurrency limit {limit} — refusing call {call_id}"
    if call_id in active:
        return f"call {call_id} already active — refusing duplicate request"
    if len(active) >= limit:
        return f"concurrency limit reached ({len(active)}/{limit} active) — refusing call {call_id}"
    return None


def resolve_request_env(cfg: RunnerConfig, request: SpawnRequest, key: str) -> str | None:
    if key in request.env:
        return request.env[key]
    return cfg.ambient_env.get(key)


def build_spawn_args(cfg: RunnerConfig, request: SpawnRequest) -> list[str]:
    """Full `docker run -d` argv for one bot, envelope flags included.

    Envelope numbers mirror bot/docker-compose.yml — the compose/runner
    agreement is locked by tests/bot/test_runner.py. `command` (when set) is
    appended after the image as the container command — it overrides the
    shipped entrypoint only on entrypoint-less images (docker-level tests
    use busybox); real runs leave it None so the shipped entrypoint runs.

    NOTE: no `--rm` here, unlike `make bot-run` — the docker engine rejects
    `--rm` together with a restart policy, and the bounded restart is the
    safety-critical half. Cleanup of finished/exited containers is the
    reconcile pass's job (`docker rm`, exit code logged first); worst case
    an exited shell sits diskless for one poll interval.
    """
    args = [
        "docker",
        "run",
        "-d",
        "--init",
        "--shm-size=1g",
        f"--name={container_name(request.call_id)}",
        f"--memory={MEM_LIMIT}",
        f"--cpus={CPUS}",
        f"--pids-limit={PIDS_LIMIT}",
        f"--restart={RESTART_POLICY}",
        "--log-driver",
        "json-file",
        "--log-opt",
        f"max-size={LOG_MAX_SIZE}",
        "--log-opt",
        f"max-file={LOG_MAX_FILE}",
    ]
    env_values: dict[str, str] = {
        "MEETING_URL": request.meeting_url,
        "CALL_ID": request.call_id,
        # Operator-level consent attestation (see module docstring).
        "CONSENT_ACK": "true",
    }
    for key in _ENV_PASSTHROUGH:
        value = resolve_request_env(cfg, request, key)
        if value is not None:
            env_values[key] = value
    debug = request.debug_host_path or cfg.debug_host_path
    if debug:
        env_values["DEBUG_DIR"] = "/debug"
    for key, value in env_values.items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{cfg.audio_host_path}:/audio"]
    profile = request.profile_host_path or cfg.profile_host_path
    if profile:
        profile_dir = env_values.get("BOT_PROFILE_DIR", "/profile")
        args += ["-v", f"{profile}:{profile_dir}"]
    if debug:
        args += ["-v", f"{debug}:/debug"]
    args.append(cfg.image)
    if request.command:
        args += list(request.command)
    return args


_EXITED_STATUS_RE = re.compile(r"^(?:Exited|Dead) \((-?\d+)\)")


def parse_container_statuses(docker_stdout: str) -> dict[str, str]:
    """Map container name -> status string from a tab-separated ps listing."""
    statuses: dict[str, str] = {}
    for line in docker_stdout.splitlines():
        name, _, status = line.partition("\t")
        if name.strip():
            statuses[name.strip()] = status.strip()
    return statuses


def reconcile_plan(
    active: Sequence[str], statuses: Mapping[str, str]
) -> tuple[list[str], list[tuple[str, int | None]]]:
    """Decide survivors vs. reaped entries for the reconcile pass.

    Returns (surviving_call_ids, reaped) where each reaped entry is
    (call_id, exit_code_if_known). Exited/Dead ⇒ reap after logging the
    exit code and `docker rm` the finished container (the runner spawns
    without --rm because the engine forbids it alongside a restart
    policy). Gone ⇒ reap too: covers containers removed out from under us
    (operator cleanup, host docker GC) or the runner having died between
    an id reservation and the actual spawn.
    "Restarting" is transient and kept: the restart policy is bounded
    (on-failure:2), so a doomed bot frees its slot on a later tick.
    """
    survivors: list[str] = []
    reaped: list[tuple[str, int | None]] = []
    for call_id in active:
        status = statuses.get(container_name(call_id))
        if status is None:
            reaped.append((call_id, None))
            continue
        match = _EXITED_STATUS_RE.match(status)
        if match:
            reaped.append((call_id, int(match.group(1))))
            continue
        survivors.append(call_id)
    return survivors, reaped


# ---------------------------------------------------------------------------
# lockfile
# ---------------------------------------------------------------------------


def load_lock(path: str) -> list[str]:
    """Read the active-call-id list; missing or corrupt files are empty.

    A corrupt lockfile is logged and treated as empty — the reconcile pass
    cannot know what was in it, but containers outlive the file and the
    ceiling is re-derived from reality on the next tick.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("lockfile %s is corrupt — starting with an empty active list", path)
        return []
    if isinstance(data, list) and all(isinstance(item, str) for item in data):
        return list(data)
    logger.warning("lockfile %s has unexpected shape — starting with an empty list", path)
    return []


def save_lock(path: str, ids: Sequence[str]) -> None:
    """Atomic write (tmp + rename) so a crash can never truncate the lock."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(list(ids), indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def acquire_runner_hold(hold_path: str) -> int | None:
    """Take the process-lifetime single-instance hold, or report it taken.

    Returns the open fd on success (caller keeps it for the process
    lifetime; closing releases the hold) and None when another runner
    instance already holds the file. The hold lives in a separate
    `<lock>.hold` file — save_lock() renames the JSON lock itself, which
    would silently defeat a flock taken on it. Linux (fcntl); every
    surface this runner targets is Linux.
    """
    fd = os.open(hold_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# ---------------------------------------------------------------------------
# docker plumbing (injectable so tests can fake the daemon)
# ---------------------------------------------------------------------------


def _subprocess_docker(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def list_bot_containers(docker: DockerCommand) -> dict[str, str]:
    result = docker(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name={CONTAINER_PREFIX}",
            "--format",
            "{{.Names}}\t{{.Status}}",
        ]
    )
    if result.returncode != 0:
        raise SpawnError(f"docker ps failed: {result.stderr.strip()}")
    return parse_container_statuses(result.stdout)


def ensure_audio_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise SpawnError(f"cannot create audio dir {path}: {exc}") from exc
    with contextlib.suppress(OSError):
        os.chmod(path, 0o777)  # container uid 1000 must be able to write the WAV
    if not os.access(path, os.W_OK):
        raise SpawnError(f"audio dir {path} is not writable by the runner")


def reconcile(cfg: RunnerConfig, docker: DockerCommand, active: Sequence[str]) -> list[str]:
    statuses = list_bot_containers(docker)
    survivors, reaped = reconcile_plan(active, statuses)
    for call_id, exit_code in reaped:
        if exit_code is None:
            logger.info("reconcile: container for call %s is gone — reaped lock entry", call_id)
            continue
        name = container_name(call_id)
        logger.info(
            "reconcile: container %s exited (code %s) — removing and reaping lock entry",
            name,
            exit_code,
        )
        result = docker(["docker", "rm", "-f", name])
        if result.returncode != 0:
            logger.debug("docker rm %s said: %s", name, result.stderr.strip())
    save_lock(cfg.lock_path, survivors)
    return survivors


def spawn(cfg: RunnerConfig, docker: DockerCommand, request: SpawnRequest) -> None:
    ensure_audio_dir(cfg.audio_host_path)
    auth_mode = resolve_request_env(cfg, request, "BOT_AUTH_MODE")
    if auth_mode == "authenticated" and not (request.profile_host_path or cfg.profile_host_path):
        logger.warning(
            "call %s: BOT_AUTH_MODE=authenticated but no profile host path set "
            "(BOT_PROFILE / request 'profile') — the bot will fail its session gate",
            request.call_id,
        )
    args = build_spawn_args(cfg, request)
    result = docker(args)
    if result.returncode != 0:
        raise SpawnError(f"docker run failed for call {request.call_id}: {result.stderr.strip()}")
    logger.info(
        "spawned %s for call %s (image %s)",
        container_name(request.call_id),
        request.call_id,
        cfg.image,
    )


def try_spawn(
    cfg: RunnerConfig,
    docker: DockerCommand,
    active: list[str],
    request: SpawnRequest,
) -> str | None:
    """Admit (id reserved in the lockfile before the container exists, so a
    crash between the two cannot leak an uncounted slot) or refuse. Returns
    None on success, the refusal/error reason otherwise — lockfile-write
    failures included, so no caller path can raise into the main loop."""
    refusal = admit(active, cfg.concurrency, request.call_id)
    if refusal is not None:
        return refusal
    active.append(request.call_id)
    try:
        save_lock(cfg.lock_path, active)
    except OSError as exc:
        active.remove(request.call_id)
        return f"spawn failed for call {request.call_id}: cannot write lockfile: {exc}"
    try:
        spawn(cfg, docker, request)
    except (SpawnError, OSError) as exc:
        active.remove(request.call_id)
        with contextlib.suppress(OSError):
            save_lock(cfg.lock_path, active)  # best effort; next tick re-derives
        return f"spawn failed for call {request.call_id}: {exc}"
    return None


# ---------------------------------------------------------------------------
# queue file + interactive commands
# ---------------------------------------------------------------------------


def parse_request(raw: str) -> SpawnRequest:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    url = data.get("meeting_url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("'meeting_url' is required")
    call_id = data.get("call_id")
    if call_id is not None and (not isinstance(call_id, str) or not call_id.strip()):
        raise ValueError("'call_id' must be a non-empty string when present")
    raw_env = data.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
    ):
        raise ValueError("'env' must be an object of string keys and values")
    raw_command = data.get("command")
    if raw_command is not None and (
        not isinstance(raw_command, list) or not all(isinstance(c, str) for c in raw_command)
    ):
        raise ValueError("'command' must be a list of strings when present")
    return SpawnRequest(
        meeting_url=url.strip(),
        call_id=(call_id.strip() if isinstance(call_id, str) else "") or uuid.uuid4().hex,
        env=dict(raw_env),
        profile_host_path=_optional_str(data.get("profile")),
        debug_host_path=_optional_str(data.get("debug")),
        command=tuple(raw_command) if isinstance(raw_command, list) else None,
        raw=raw,
    )


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_queue_lines(path: str, offset: int) -> tuple[list[str], int]:
    """New JSONL lines since `offset`, plus the new offset.

    A file that shrank (rotated/rewritten) is re-read from the start. Lines
    are consumed as they land; a partially written line fails to parse and
    is rejected like any other malformed request — acceptable for a
    precursor queue.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    lines: list[str] = []
    with open(path, encoding="utf-8") as handle:
        handle.seek(offset)
        for line in handle:
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
        new_offset = handle.tell()
    return lines, new_offset


def append_rejected(queue_path: str, reason: str, raw: str) -> None:
    path = Path(queue_path + ".rejected")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"reason": reason, "request": raw}) + "\n")


def process_queue_file(
    cfg: RunnerConfig, docker: DockerCommand, active: list[str], offset: int
) -> int:
    path = cfg.queue_file
    assert path is not None
    lines, new_offset = read_queue_lines(path, offset)
    for raw in lines:
        try:
            request = parse_request(raw)
        except ValueError as exc:
            reason = f"malformed request: {exc}"
            logger.error("queue: %s", reason)
            append_rejected(path, reason, raw)
            continue
        failure = try_spawn(cfg, docker, active, request)
        if failure is not None:
            logger.error("queue: %s", failure)
            append_rejected(path, failure, raw)
    return new_offset


def _stdin_reader(commands: queue.Queue[str], quit_on_eof: bool) -> None:
    for line in sys.stdin:
        commands.put(line.rstrip("\n"))
        if line.lower().strip() in {"quit", "exit"}:
            return
    if quit_on_eof:
        commands.put("quit")


def handle_command(
    cfg: RunnerConfig,
    docker: DockerCommand,
    active: list[str],
    line: str,
) -> bool:
    """One interactive command; returns False to stop the loop."""
    parts = line.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if command == "join":
        if not arg:
            logger.error("usage: join <MEETING_URL>")
            return True
        request = SpawnRequest(meeting_url=arg, call_id=uuid.uuid4().hex)
        reason = try_spawn(cfg, docker, active, request)
        if reason is not None:
            logger.error("%s", reason)
        else:
            logger.info("active %d/%d: %s", len(active), cfg.concurrency, request.call_id)
        return True
    if command == "status":
        logger.info("active %d/%d: %s", len(active), cfg.concurrency, ", ".join(active) or "none")
        return True
    if command in {"quit", "exit"}:
        return False
    logger.error("unknown command %r — try: join <MEETING_URL> | status | quit", line)
    return True


def setup_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO
    logging.basicConfig(level=resolved, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run(cfg: RunnerConfig, docker: DockerCommand | None = None) -> int:
    command = docker or _subprocess_docker
    hold_path = cfg.lock_path + ".hold"
    hold_fd = acquire_runner_hold(hold_path)
    if hold_fd is None:
        logger.error(
            "another runner instance is already running (hold: %s) — the "
            "ceiling is per-instance, so exactly one runner may run",
            hold_path,
        )
        return 1
    stop_event = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        if stop_event.is_set():  # duplicate signals (process-group TERM) log once
            return
        logger.info(
            "received signal %s — shutting down (bots keep running; they are detached)", signum
        )
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):  # not the main thread (tests)
            signal.signal(sig, _stop)

    try:
        active = reconcile(cfg, command, load_lock(cfg.lock_path))
    except (SpawnError, OSError) as exc:
        logger.error("cannot start — docker unreachable or lock unreadable: %s", exc)
        return 1
    logger.info(
        "runner started (precursor — PR 5's DB-driven runner subsumes this): "
        "image=%s limit=%d active=%d lock=%s",
        cfg.image,
        cfg.concurrency,
        len(active),
        cfg.lock_path,
    )
    logger.info("audio volume host path: %s", cfg.audio_host_path)
    if cfg.queue_file:
        logger.info("draining queue file %s", cfg.queue_file)
    logger.info("commands: join <MEETING_URL> | status | quit")

    commands: queue.Queue[str] = queue.Queue()
    # Pure-interactive runs stop when stdin closes (terminal gone). A queue
    # file is a daemon source — EOF on stdin must not kill the drain loop;
    # SIGINT/SIGTERM stops it.
    threading.Thread(
        target=_stdin_reader, args=(commands, cfg.queue_file is None), daemon=True
    ).start()

    offset = 0
    next_tick = time.monotonic() + POLL_INTERVAL_S  # startup pass already ran
    while not stop_event.is_set():
        try:
            line = commands.get(timeout=0.5)
        except queue.Empty:
            line = ""
        if line and not handle_command(cfg, command, active, line):
            stop_event.set()
        if cfg.queue_file and not stop_event.is_set():
            try:
                offset = process_queue_file(cfg, command, active, offset)
            except (SpawnError, OSError) as exc:  # docker/queue hiccups — retry next tick
                logger.error("queue poll skipped: %s", exc)
        now = time.monotonic()
        if now >= next_tick:
            next_tick = now + POLL_INTERVAL_S
            try:
                active = reconcile(cfg, command, active)
            except (SpawnError, OSError) as exc:
                logger.error("reconcile skipped: %s", exc)
    logger.info(
        "runner stopped; %d bot container(s) left running under %s "
        "(a later runner start reconciles them)",
        len(active),
        cfg.lock_path,
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bot.runner",
        description=(
            "Precursor bot-runner orchestrator (PR 4): docker-run spawner with "
            "a hard concurrency ceiling and lockfile reconcile. PR 5's "
            "DB-driven runner subsumes this script."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="active-bot ceiling (default: CALL_CONCURRENCY_LIMIT env, else 3)",
    )
    parser.add_argument("--queue-file", default=None, help="JSONL request queue to drain")
    parser.add_argument(
        "--image",
        default=None,
        help="bot image override (default oreeai-bot:BOT_IMAGE_TAG)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = RunnerConfig.from_env(
        os.environ,
        concurrency=args.concurrency,
        image=args.image,
        queue_file=args.queue_file,
    )
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    if cfg.concurrency < 1:
        logger.error("concurrency limit must be >= 1")
        return 1
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
