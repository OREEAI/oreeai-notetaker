"""Tier-2 docker tests for the PR 4 precursor runner (docker marker).

End-to-end mirrors of the manual "You test this" scenarios that do not
need Meet or the bot image: the runner's real spawn path runs against
busybox / python:3.13-slim containers carrying the production resource
envelope, and `docker inspect` proves the numbers. The memory-bomb test
(S3) is opt-in with RUNNER_BOMB_TEST=1 so CI never allocates or pulls for
it by default. Skips cleanly when no docker daemon is reachable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from bot.runner import RunnerConfig, SpawnRequest, list_bot_containers, reconcile, try_spawn

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

BOMB_SOURCE = "a=[]\nwhile True: a.append(bytearray(10**7))"


def _docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)


def docker_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The runner-injection shape (full argv list), like _subprocess_docker."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@pytest.fixture(scope="session")
def docker_ready() -> None:
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon not reachable")


@pytest.fixture
def names() -> Iterator[list[str]]:
    spawned: list[str] = []
    yield spawned
    for name in spawned:
        _docker("rm", "-f", name)


@pytest.fixture
def cfg(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        image="busybox:latest",
        concurrency=3,
        audio_host_path=str(tmp_path / "audio"),
        lock_path=str(tmp_path / "runner.lock"),
        env={},
    )


def request_for(call_id: str, command: Sequence[str]) -> SpawnRequest:
    return SpawnRequest(
        meeting_url="https://meet.google.com/abc-defg-hij",
        call_id=call_id,
        command=tuple(command),
    )


def inspect_doc(name: str) -> dict[str, object]:
    result = _docker("inspect", name)
    assert result.returncode == 0, result.stderr
    doc: dict[str, object] = json.loads(result.stdout)[0]
    return doc


def wait_until(predicate: Callable[[], bool], timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1.0)
    return False


# manual scenario 1 — docker inspect shows the envelope on a runner-spawned bot
def test_spawned_container_carries_resource_limits(
    cfg: RunnerConfig, names: list[str], docker_ready: None
) -> None:
    call_id = uuid.uuid4().hex
    active: list[str] = []
    assert try_spawn(cfg, docker_argv, active, request_for(call_id, ["sleep", "300"])) is None
    name = f"oreeai-bot-{call_id}"
    names.append(name)
    doc = inspect_doc(name)
    host_config = doc["HostConfig"]
    assert isinstance(host_config, dict)
    assert host_config["Memory"] == 2 * 1024**3
    assert host_config["NanoCpus"] == 1_500_000_000
    assert host_config["PidsLimit"] == 512
    restart = host_config["RestartPolicy"]
    assert isinstance(restart, dict)
    assert restart["Name"] == "on-failure"
    assert restart["MaximumRetryCount"] == 2
    log_config = host_config["LogConfig"]
    assert isinstance(log_config, dict)
    assert log_config["Type"] == "json-file"
    assert log_config["Config"] == {"max-size": "10m", "max-file": "3"}
    state = doc["State"]
    assert isinstance(state, dict) and state["Running"] is True
    assert active == [call_id]


def test_compose_file_is_schema_valid(docker_ready: None) -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(REPO_ROOT / "bot" / "docker-compose.yml"), "config", "-q"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MEETING_URL": "https://meet.google.com/abc-defg-hij"},
    )
    assert result.returncode == 0, result.stderr


# manual scenario 2 — three held, fourth refused with no fourth container
def test_fourth_bot_refused_without_container(
    cfg: RunnerConfig, names: list[str], docker_ready: None
) -> None:
    active: list[str] = []
    held: list[str] = []
    for _ in range(3):
        call_id = uuid.uuid4().hex
        assert try_spawn(cfg, docker_argv, active, request_for(call_id, ["sleep", "300"])) is None
        held.append(call_id)
        names.append(f"oreeai-bot-{call_id}")
    fourth = uuid.uuid4().hex
    reason = try_spawn(cfg, docker_argv, active, request_for(fourth, ["sleep", "300"]))
    assert reason is not None
    assert "concurrency limit reached (3/3 active)" in reason
    statuses = list_bot_containers(docker_argv)
    assert all(f"oreeai-bot-{c}" in statuses for c in held)  # first three keep running
    assert f"oreeai-bot-{fourth}" not in statuses  # no 4th container was created
    assert reconcile(cfg, docker_argv, active) == active  # the sweep keeps all three


# manual scenarios 3 + 5 — memory bomb OOM-killed only itself; stays down.
# Opt-in: RUNNER_BOMB_TEST=1 (pulls python:3.13-slim ~50 MB, allocates 2 GB).
@pytest.mark.skipif(
    os.environ.get("RUNNER_BOMB_TEST") != "1",
    reason="opt-in: set RUNNER_BOMB_TEST=1 (needs local python:3.13-slim image)",
)
def test_memory_bomb_kills_only_the_bombed_bot(
    cfg: RunnerConfig, names: list[str], docker_ready: None
) -> None:
    neighbor_call = uuid.uuid4().hex
    bomb_call = uuid.uuid4().hex
    bomb_cfg = replace(cfg, image="python:3.13-slim")
    active: list[str] = []
    assert try_spawn(cfg, docker_argv, active, request_for(neighbor_call, ["sleep", "600"])) is None
    bomb_request = request_for(bomb_call, ["python3", "-c", BOMB_SOURCE])
    assert try_spawn(bomb_cfg, docker_argv, active, bomb_request) is None
    names.extend([f"oreeai-bot-{neighbor_call}", f"oreeai-bot-{bomb_call}"])

    def bomb_gone() -> bool:
        survivors = reconcile(bomb_cfg, docker_argv, active)
        active[:] = survivors
        return bomb_call not in survivors and neighbor_call in survivors

    # restart policy is bounded (on-failure:2): retries burn through the same
    # bomb within seconds; once exhausted the container sits Exited(137)
    # until a reconcile tick removes it. The slot must free, the neighbor
    # must survive the whole storm.
    assert wait_until(bomb_gone, timeout_s=120.0), "bombed container never went away"
    neighbor = inspect_doc(f"oreeai-bot-{neighbor_call}")
    state = neighbor["State"]
    assert isinstance(state, dict) and state["Running"] is True
    # ... and it does not come back: "stays down" (scenario 5)
    time.sleep(3.0)
    assert bomb_call not in list_bot_containers(docker_argv)


# manual scenario 6 — SIGKILLed runner: stale entries reaped on next reconcile
def test_reconcile_reaps_phantom_and_killed_containers(
    cfg: RunnerConfig, names: list[str], docker_ready: None
) -> None:
    call_id = uuid.uuid4().hex
    active: list[str] = []
    assert try_spawn(cfg, docker_argv, active, request_for(call_id, ["sleep", "300"])) is None
    name = f"oreeai-bot-{call_id}"
    names.append(name)
    # simulate a lockfile from a runner that died: one phantom entry + the real one
    lock_file = Path(cfg.lock_path)
    lock_file.write_text(json.dumps([call_id, "stale-from-dead-runner"]) + "\n", "utf-8")
    survivors = reconcile(cfg, docker_argv, json.loads(lock_file.read_text("utf-8")))
    assert survivors == [call_id]  # phantom gone -> reaped; live bot kept
    assert _docker("kill", name).returncode == 0

    def gone() -> bool:
        return reconcile(cfg, docker_argv, survivors) == []

    assert wait_until(gone, timeout_s=15.0), "killed container not reaped by reconcile"
    assert json.loads(lock_file.read_text("utf-8")) == []
