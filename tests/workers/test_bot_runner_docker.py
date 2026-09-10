"""Tier-2 docker tests for the PR 5 service runner (docker marker).

Mirrors the manual scenarios that do not need Meet or the bot image: the
runner's real spawn argv runs a tiny stand-in container carrying the
production resource envelope, and `docker inspect` proves the numbers
(including `--rm` with no restart policy — the combination the PR 4
handoff proved mutually exclusive). `docker compose config --format json`
validates both compose files and proves the docker socket is mounted only
on the bot-runner service. Skips cleanly when no docker daemon is
reachable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import oreeai_notetaker.workers.bot_runner as br
from oreeai_notetaker.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]


def _docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)


@pytest.fixture(scope="session")
def docker_ready() -> None:
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon not reachable")


@pytest.fixture
def names() -> list[str]:
    spawned: list[str] = []
    yield spawned
    for name in spawned:
        _docker("rm", "-f", name)


def make_spawn_args_call(call_id: uuid.UUID) -> Any:
    call = br.Call(
        meeting_url="https://meet.google.com/abc-defg-hij",
        user_ref="test-user-1",
        consent_ack=True,
        webhook_url="http://receiver.example/hooks/x",
        webhook_secret="shhhhhhhhhhhhhhhh",
    )
    call.id = call_id
    return call


def inspect_container(name: str) -> dict[str, Any]:
    result = _docker("inspect", name)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)[0]


def compose_config_json(
    compose_file: str, extra_env: dict[str, str] | None = None
) -> dict[str, Any]:
    env = dict(os.environ)
    env.setdefault("MEETING_URL", "https://meet.google.com/abc-defg-hij")
    env.setdefault("DATABASE_URL", "postgresql+asyncpg://oreeai:oreeai@db:5432/oreeai")
    env.setdefault("REDIS_URL", "redis://redis:6379/0")
    env.setdefault("API_KEY", "compose-config-validation-key")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def has_docker_socket(service: dict[str, Any]) -> bool:
    return any("docker.sock" in v.get("source", "") for v in service.get("volumes", []))


@pytest.mark.usefixtures("docker_ready", "names")
def test_spawn_carries_production_envelope(
    monkeypatch: pytest.MonkeyPatch, names: list[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "bot_docker_network", "bridge")
    monkeypatch.setattr(settings, "audio_host_path", str(tmp_path / "audio"))
    monkeypatch.setattr(br, "container_name", lambda call_id: f"oreeai-bot-inspect-{call_id}")

    call_id = uuid.uuid4()
    args = br.build_spawn_args(
        make_spawn_args_call(call_id), image="busybox", command=["sleep", "30"]
    )
    run = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    names.append(f"oreeai-bot-inspect-{call_id}")
    try:
        # docker run --rm blocks until exit and then auto-removes, so the
        # container must be inspected while it is still running.
        deadline = 30.0
        data: dict[str, Any] | None = None
        while deadline > 0:
            result = _docker("inspect", f"oreeai-bot-inspect-{call_id}")
            if result.returncode == 0:
                data = json.loads(result.stdout)[0]
                break
            if run.poll() is not None:
                pytest.fail(f"container exited before inspection: {run.stderr.read()!r}")
            time.sleep(0.5)
            deadline -= 0.5
        assert data is not None, "container never became inspectable"
    finally:
        run.kill()
        run.wait()

    host_config = data["HostConfig"]
    assert host_config["Memory"] == 2147483648
    assert host_config["NanoCpus"] == 1_500_000_000
    assert host_config["PidsLimit"] == 512
    log_config = host_config["LogConfig"]
    assert log_config["Type"] == "json-file"
    assert log_config["Config"]["max-size"] == "10m"
    assert log_config["Config"]["max-file"] == "3"
    assert host_config["AutoRemove"] is True
    # docker renders the default policy as "no"; either way nothing restarts
    assert host_config["RestartPolicy"]["Name"] in ("", "no")
    assert host_config["NetworkMode"] == "bridge"
    mounts = {m["Destination"]: m["Source"] for m in data["Mounts"]}
    assert mounts["/audio"] == str(tmp_path / "audio")
    env_pairs = ";".join(data["Config"]["Env"])
    assert "CONSENT_ACK=true" in env_pairs
    assert f"CALL_ID={call_id}" in env_pairs
    assert "MEETING_URL=https://meet.google.com/abc-defg-hij" in env_pairs


@pytest.mark.usefixtures("docker_ready")
def test_bot_compose_schema_valid_and_socket_only_on_runner() -> None:
    config = compose_config_json("bot/docker-compose.yml")
    services = config["services"]
    assert set(services) >= {"bot", "bot-runner"}
    assert not has_docker_socket(services["bot"])
    assert has_docker_socket(services["bot-runner"])


@pytest.mark.usefixtures("docker_ready")
def test_dev_compose_schema_valid_and_socket_only_on_runner() -> None:
    config = compose_config_json("docker-compose.yml")
    services = config["services"]
    assert set(services) >= {"api", "db", "redis", "bot-runner"}
    assert not has_docker_socket(services["api"])
    assert has_docker_socket(services["bot-runner"])
