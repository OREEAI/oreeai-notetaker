"""Tier-1 (no docker) tests for the PR 4 precursor runner.

These mirror the manual "You test this" scenarios at the logic level —
envelope flags, ceiling refusal, reconcile planning, lockfile semantics —
so the docker-free CI lane proves the mechanics. The docker-backed
end-to-end mirrors live in tests/bot/test_runner_docker.py (docker marker).
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from bot.runner import (
    CPUS,
    MEM_LIMIT,
    PIDS_LIMIT,
    RESTART_POLICY,
    RunnerConfig,
    SpawnRequest,
    admit,
    build_spawn_args,
    container_name,
    load_lock,
    parse_container_statuses,
    parse_request,
    process_queue_file,
    read_queue_lines,
    reconcile,
    reconcile_plan,
    save_lock,
    try_spawn,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeDocker:
    """Stands in for the docker CLI; records every argv it was called with."""

    def __init__(self, ps_statuses: dict[str, str] | None = None, run_rc: int = 0) -> None:
        self.ps_statuses = ps_statuses or {}
        self.run_rc = run_rc
        self.calls: list[list[str]] = []
        self.removed: list[str] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        if args[:2] == ["docker", "ps"]:
            stdout = "".join(f"{n}\t{s}\n" for n, s in self.ps_statuses.items())
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[:2] == ["docker", "rm"]:
            self.removed.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["docker", "run"]:
            if self.run_rc == 0:
                return subprocess.CompletedProcess(args, 0, "cafebabe\n", "")
            return subprocess.CompletedProcess(args, self.run_rc, "", "simulated daemon error")
        raise AssertionError(f"unexpected docker call: {args}")

    def run_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


def make_cfg(tmp_path: Path, **overrides: object) -> RunnerConfig:
    base: dict[str, object] = {
        "image": "oreeai-bot:local",
        "concurrency": 3,
        "audio_host_path": str(tmp_path / "audio"),
        "lock_path": str(tmp_path / "runner.lock"),
        "env": {},
    }
    base.update(overrides)
    return RunnerConfig(**base)  # type: ignore[arg-type]


def make_request(call_id: str = "call-1", **overrides: object) -> SpawnRequest:
    base: dict[str, object] = {
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "call_id": call_id,
    }
    base.update(overrides)
    return SpawnRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# admission / ceiling (manual scenario 2, logic level)
# ---------------------------------------------------------------------------


def test_admit_under_and_at_limit() -> None:
    assert admit(["a", "b"], 3, "c") is None
    refusal = admit(["a", "b", "c"], 3, "d")
    assert refusal is not None
    assert "concurrency limit reached (3/3 active)" in refusal
    assert "refusing call d" in refusal


def test_admit_rejects_duplicate_call_id() -> None:
    refusal = admit(["a"], 3, "a")
    assert refusal is not None
    assert "already active" in refusal


def test_admit_rejects_invalid_limit() -> None:
    refusal = admit([], 0, "a")
    assert refusal is not None
    assert "invalid concurrency limit" in refusal


# ---------------------------------------------------------------------------
# spawn argv / envelope (manual scenario 1, flag level)
# ---------------------------------------------------------------------------


def test_spawn_args_pin_resource_envelope() -> None:
    cfg = make_cfg(Path("/tmp"))
    args = build_spawn_args(cfg, make_request())
    assert args[0] == "docker" and args[1] == "run" and "-d" in args
    # No --rm: the docker engine forbids it alongside a restart policy, and
    # cleanup of finished containers is the reconcile pass's job.
    assert "--rm" not in args and "--init" in args and "--shm-size=1g" in args
    assert f"--name={container_name('call-1')}" in args
    assert f"--memory={MEM_LIMIT}" in args  # 2g
    assert f"--cpus={CPUS}" in args  # 1.5
    assert f"--pids-limit={PIDS_LIMIT}" in args  # 512
    assert f"--restart={RESTART_POLICY}" in args  # on-failure:2 — never always
    assert args[args.index("--log-driver") :][:2] == ["--log-driver", "json-file"]
    assert "max-size=10m" in args and "max-file=3" in args
    assert f"{cfg.audio_host_path}:/audio" in args


def test_spawn_args_never_publish_ports() -> None:
    args = build_spawn_args(make_cfg(Path("/tmp")), make_request())
    assert "-p" not in args and "--publish" not in args
    assert not any(a.startswith("-p=") or a.startswith("--publish=") for a in args)


def test_spawn_args_sets_consent_and_identity_env() -> None:
    args = build_spawn_args(make_cfg(Path("/tmp")), make_request())
    env_flags = dict(
        (args[i + 1].split("=", 1)[0], args[i + 1].split("=", 1)[1])
        for i, a in enumerate(args)
        if a == "-e"
    )
    assert env_flags["CONSENT_ACK"] == "true"  # operator-level attestation
    assert env_flags["MEETING_URL"] == "https://meet.google.com/abc-defg-hij"
    assert env_flags["CALL_ID"] == "call-1"


def test_spawn_args_forwards_allowlisted_ambient_env() -> None:
    cfg = make_cfg(
        Path("/tmp"),
        env={"BOT_AUTH_MODE": "authenticated", "DATABASE_URL": "postgres://secret"},
    )
    args = build_spawn_args(cfg, make_request())
    joined = " ".join(args)
    assert "-e BOT_AUTH_MODE=authenticated" in joined
    assert "postgres://secret" not in joined  # allowlist, not blanket passthrough


def test_spawn_args_request_env_overrides_ambient() -> None:
    cfg = make_cfg(Path("/tmp"), env={"BOT_AUTH_MODE": "authenticated"})
    request = make_request(env={"BOT_AUTH_MODE": "anonymous"})
    args = build_spawn_args(cfg, request)
    assert "-e BOT_AUTH_MODE=anonymous" in " ".join(args)


def test_spawn_args_mounts_profile_at_container_profile_dir() -> None:
    cfg = make_cfg(Path("/tmp"), profile_host_path="/home/op/chrome-profile", env={})
    args = build_spawn_args(cfg, make_request())
    assert "/home/op/chrome-profile:/profile" in args
    request = make_request(profile_host_path="/copy-two/chrome-profile")
    args = build_spawn_args(cfg, request)  # per-request profile wins (scenario 2)
    assert "/copy-two/chrome-profile:/profile" in args


def test_spawn_args_mounts_debug_dir_when_configured() -> None:
    cfg = make_cfg(Path("/tmp"), debug_host_path="/tmp/dbg")
    args = build_spawn_args(cfg, make_request())
    assert "/tmp/dbg:/debug" in args
    assert "-e DEBUG_DIR=/debug" in " ".join(args)


def test_spawn_args_appends_test_command_after_image() -> None:
    cfg = make_cfg(Path("/tmp"), image="busybox:latest")
    args = build_spawn_args(cfg, make_request(command=("sleep", "120")))
    assert args[-3:] == ["busybox:latest", "sleep", "120"]


# ---------------------------------------------------------------------------
# compose/runner envelope agreement (drift lock)
# ---------------------------------------------------------------------------


def test_compose_file_carries_the_same_envelope() -> None:
    text = (REPO_ROOT / "bot" / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"mem_limit: {MEM_LIMIT}" in text
    assert f'cpus: "{CPUS}"' in text
    assert f"pids_limit: {PIDS_LIMIT}" in text
    assert f"restart: {RESTART_POLICY}" in text
    assert 'max-size: "10m"' in text and 'max-file: "3"' in text
    assert "/var/lib/oreeai/audio:/audio" in text
    assert "\n    ports:" not in text and "\n    expose:" not in text


def test_restart_policy_bounded_never_always() -> None:
    assert RESTART_POLICY == "on-failure:2"
    text = (REPO_ROOT / "bot" / "docker-compose.yml").read_text(encoding="utf-8")
    yaml_restarts = [
        line.strip() for line in text.splitlines() if line.strip().startswith("restart:")
    ]
    assert yaml_restarts == [f"restart: {RESTART_POLICY}"]


# ---------------------------------------------------------------------------
# lockfile
# ---------------------------------------------------------------------------


def test_lockfile_roundtrip(tmp_path: Path) -> None:
    path = str(tmp_path / "runner.lock")
    assert load_lock(path) == []  # missing file = empty
    save_lock(path, ["a", "b"])
    assert load_lock(path) == ["a", "b"]
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_lockfile_corrupt_is_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "runner.lock"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_lock(str(path)) == []
    assert "corrupt" in caplog.text


# ---------------------------------------------------------------------------
# reconcile (manual scenarios 3/5/6, planning level)
# ---------------------------------------------------------------------------


def test_reconcile_plan_keeps_and_reaps() -> None:
    statuses = parse_container_statuses(
        "oreeai-bot-alive\tUp 3 seconds\n"
        "oreeai-bot-restarting\tRestarting (1) 2 seconds ago\n"
        "oreeai-bot-bombed\tExited (137) 1 second ago\n"
    )
    survivors, reaped = reconcile_plan(["alive", "restarting", "bombed", "phantom"], statuses)
    assert survivors == ["alive", "restarting"]
    assert ("phantom", None) in reaped
    assert ("bombed", 137) in reaped


def test_reconcile_writes_survivors_and_removes_exited(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    docker = FakeDocker({"oreeai-bot-keep": "Up 5 seconds", "oreeai-bot-gone": "Exited (2) 1 s"})
    survivors = reconcile(cfg, docker, ["keep", "gone", "phantom"])
    assert survivors == ["keep"]
    assert docker.removed == ["oreeai-bot-gone"]
    assert load_lock(cfg.lock_path) == ["keep"]


# ---------------------------------------------------------------------------
# try_spawn bookkeeping
# ---------------------------------------------------------------------------


def test_try_spawn_success_registers_and_persists(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    docker = FakeDocker()
    active: list[str] = []
    assert try_spawn(cfg, docker, active, make_request("one")) is None
    assert active == ["one"]
    assert load_lock(cfg.lock_path) == ["one"]
    assert len(docker.run_calls()) == 1


def test_try_spawn_refusal_at_ceiling_does_not_call_docker(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    docker = FakeDocker()
    active = ["a", "b", "c"]
    reason = try_spawn(cfg, docker, active, make_request("d"))
    assert reason is not None and "concurrency limit reached" in reason
    assert docker.run_calls() == []  # no 4th container created
    assert active == ["a", "b", "c"]


def test_try_spawn_failure_rolls_back_reservation(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    docker = FakeDocker(run_rc=125)
    active: list[str] = []
    reason = try_spawn(cfg, docker, active, make_request("one"))
    assert reason is not None and "spawn failed" in reason
    assert active == []
    assert load_lock(cfg.lock_path) == []


def test_try_spawn_creates_audio_dir(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    try_spawn(cfg, FakeDocker(), [], make_request())
    assert (tmp_path / "audio").is_dir()


# ---------------------------------------------------------------------------
# queue file (manual scenario 2 via file mode)
# ---------------------------------------------------------------------------


def test_parse_request_variants() -> None:
    req = parse_request('{"meeting_url": "https://meet.google.com/abc-defg-hij"}')
    assert req.call_id and len(req.call_id) == 32  # uuid4 hex generated
    full = parse_request(
        json.dumps(
            {
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "call_id": "c1",
                "env": {"BOT_AUTH_MODE": "anonymous"},
                "profile": "/copy-two",
                "command": ["sleep", "600"],
            }
        )
    )
    assert full.call_id == "c1"
    assert full.env == {"BOT_AUTH_MODE": "anonymous"}
    assert full.profile_host_path == "/copy-two"
    assert full.command == ("sleep", "600")


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        '"just a string"',
        '{"call_id": "x"}',
        '{"meeting_url": 42}',
        '{"meeting_url": "u", "env": {"k": 3}}',
        '{"meeting_url": "u", "command": "sleep"}',
    ],
)
def test_parse_request_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_request(bad)


def test_read_queue_lines_tracks_offset(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    path.write_text('{"meeting_url": "https://meet.google.com/a-bbbb-ccc"}\n', encoding="utf-8")
    lines, offset = read_queue_lines(str(path), 0)
    assert len(lines) == 1
    lines, offset2 = read_queue_lines(str(path), offset)
    assert lines == []
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"meeting_url": "https://meet.google.com/d-eeee-fff"}\n')
    lines, _ = read_queue_lines(str(path), offset2)
    assert len(lines) == 1
    path.write_text("", encoding="utf-8")  # truncation resets the offset
    lines, _ = read_queue_lines(str(path), offset2)
    assert lines == []


def test_process_queue_file_spawns_admits_and_rejects(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.jsonl"
    queue_path.write_text(
        "\n".join(
            [
                json.dumps({"meeting_url": "https://meet.google.com/a-bbbb-ccc", "call_id": "c1"}),
                "not json at all",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, queue_file=str(queue_path), concurrency=1)
    active: list[str] = []
    docker = FakeDocker()
    offset = process_queue_file(cfg, docker, active, 0)
    assert active == ["c1"]
    assert offset > 0
    with open(queue_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"meeting_url": "https://meet.google.com/d-eeee-fff"}) + "\n")
    process_queue_file(cfg, docker, active, offset)
    assert active == ["c1"]  # second refused at the ceiling
    rejected = Path(str(queue_path) + ".rejected")
    reasons = rejected.read_text(encoding="utf-8")
    assert "malformed request" in reasons
    assert "concurrency limit reached" in reasons


# ---------------------------------------------------------------------------
# standing rules: import boundaries, ops doc completeness
# ---------------------------------------------------------------------------


def test_runner_never_imports_the_service_package() -> None:
    tree = ast.parse((REPO_ROOT / "bot" / "runner.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.split(".")[0].startswith("oreeai")]


def test_swap_doc_is_complete(tmp_path: Path) -> None:
    text = (REPO_ROOT / "ops" / "swap.md").read_text(encoding="utf-8")
    for required in ("fallocate", "mkswap", "swapon", "/etc/fstab", "chmod 600", "free -h"):
        assert required in text, f"ops/swap.md missing {required!r}"
    assert "swapoff" in text  # rollback documented
