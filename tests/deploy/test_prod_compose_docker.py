"""Tier-2 docker guards for the production compose file (PR 8).

The standing rule — **production compose never publishes ports to
0.0.0.0** (the OreeAI PR #48 lesson) — is executable here, not prose:
`docker compose config --format json` is parsed and every published port
must carry an explicit `127.0.0.1` host binding (or not exist at all).
The same run locks the rest of the deploy contract that would otherwise
rot silently: `db`/`redis` unpublished, the docker socket mounted only on
`bot-runner`, the runner's resource envelope, per-service healthchecks /
restart policies / log caps, and the profile gate on the bot build
service.

Skips cleanly when no docker daemon is reachable (mirrors
`tests/workers/test_bot_runner_docker.py`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = "docker-compose.prod.yml"
APP_SERVICES = ("api", "db", "redis", "bot-runner")

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]


@pytest.fixture(scope="session")
def docker_ready() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("docker daemon not reachable")


@pytest.fixture(scope="session")
def env_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Deterministic interpolation input.

    Passing `--env-file` replaces the project `.env`, so a developer's
    local `.env` (dev ports, `ENVIRONMENT=local`) cannot leak into the
    assertions.
    """
    path = tmp_path_factory.mktemp("prod-compose") / "compose.env"
    path.write_text(
        "API_KEY=compose-config-validation-key\n"
        "POSTGRES_PASSWORD=compose-config-validation-password\n"
        "POSTGRES_USER=oreeai\n"
        "POSTGRES_DB=oreeai\n"
    )
    return path


def _minimal_env() -> dict[str, str]:
    """Hermetic environment for `docker compose config`.

    Shell environment outranks `--env-file` during interpolation, so a
    developer's exported `API_IMAGE_TAG`/`ENVIRONMENT` would otherwise
    leak into the assertions. Only what the docker CLI needs to reach the
    daemon is passed through.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    for name in (
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
    ):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def compose_config(
    env_file: Path,
    *,
    profiles: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    argv = ["docker", "compose"]
    for profile in profiles:
        argv.extend(["--profile", profile])
    argv.extend(["-f", COMPOSE_FILE, "--env-file", str(env_file), "config", "--format", "json"])
    process_env = _minimal_env()
    if env:
        process_env.update(env)
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        env=process_env,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def config(env_file: Path, docker_ready: None) -> dict[str, Any]:
    return compose_config(env_file)


@pytest.fixture(scope="session")
def config_with_build_profile(env_file: Path, docker_ready: None) -> dict[str, Any]:
    return compose_config(env_file, profiles=("build",))


def published_ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    return service.get("ports") or []


def has_docker_socket(service: dict[str, Any]) -> bool:
    return any("docker.sock" in str(v.get("source", "")) for v in service.get("volumes", []))


def test_no_published_port_binds_anything_but_loopback(config: dict[str, Any]) -> None:
    """The PR #48 rule: every published port needs `127.0.0.1` explicitly."""
    offenders: list[str] = []
    for name, service in config["services"].items():
        for port in published_ports(service):
            if port.get("host_ip") != "127.0.0.1":
                offenders.append(f"{name}: {port}")
    assert offenders == [], f"services publish outside loopback: {offenders}"


def test_api_binds_loopback_and_db_redis_publish_nothing(config: dict[str, Any]) -> None:
    api_ports = published_ports(config["services"]["api"])
    assert len(api_ports) == 1
    assert api_ports[0]["host_ip"] == "127.0.0.1"
    assert api_ports[0]["target"] == 8000
    assert published_ports(config["services"]["db"]) == []
    assert published_ports(config["services"]["redis"]) == []


def test_docker_socket_only_on_bot_runner(config: dict[str, Any]) -> None:
    services = config["services"]
    assert has_docker_socket(services["bot-runner"])
    assert not has_docker_socket(services["api"])
    assert not has_docker_socket(services["db"])
    assert not has_docker_socket(services["redis"])


def test_bot_runner_resource_envelope(config: dict[str, Any]) -> None:
    runner = config["services"]["bot-runner"]
    assert int(runner["mem_limit"]) == 1024**3
    assert float(runner["cpus"]) == 0.5
    assert int(runner["pids_limit"]) == 256


def test_every_app_service_restarts_has_healthcheck_and_log_caps(
    config: dict[str, Any],
) -> None:
    for name in APP_SERVICES:
        service = config["services"][name]
        assert service.get("restart") == "unless-stopped", name
        healthcheck = service.get("healthcheck") or {}
        assert healthcheck.get("test"), f"{name} has no healthcheck"
        logging = service.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging["options"]["max-size"] == "10m", name
        assert logging["options"]["max-file"] == "5", name


def test_api_healthcheck_sends_api_key(config: dict[str, Any]) -> None:
    test = " ".join(config["services"]["api"]["healthcheck"]["test"])
    assert "/api/v1/health" in test
    assert "X-API-Key" in test
    assert "$API_KEY" in test.replace("$$", "$")


def test_default_config_runs_exactly_the_four_services(config: dict[str, Any]) -> None:
    assert set(config["services"]) == set(APP_SERVICES)


def test_bot_build_service_is_profile_gated_and_unpublished(
    config_with_build_profile: dict[str, Any],
) -> None:
    service = config_with_build_profile["services"]["bot"]
    assert service["profiles"] == ["build"]
    assert published_ports(service) == []
    assert not has_docker_socket(service)
    assert service["image"] == "oreeai-bot:v0.1.0"
    build = service["build"]
    assert build["dockerfile"] == "bot/Dockerfile"


def test_network_and_volume_keep_their_contract_names(config: dict[str, Any]) -> None:
    # The runner spawns bots onto `oreeai_internal`; a compose-renamed
    # network would make every spawn die exit 125.
    assert config["networks"]["oreeai_internal"]["name"] == "oreeai_internal"
    assert config["volumes"]["pgdata"]["name"] == "oreeai_pgdata"


def test_runner_gets_the_storage_and_transcription_env(config: dict[str, Any]) -> None:
    environment = config["services"]["bot-runner"]["environment"]
    for required in (
        "S3_ENDPOINT_URL",
        "S3_REGION",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "TRANSCRIPTION_PROVIDER",
        "DEEPGRAM_API_KEY",
        "API_KEY",
        "AUDIO_HOST_PATH",
        "BOT_IMAGE_TAG",
        "BOT_DOCKER_NETWORK",
        "CALL_CONCURRENCY_LIMIT",
    ):
        assert required in environment, required


def test_api_and_runner_share_cache_env(config: dict[str, Any]) -> None:
    """The runner writes `<CACHE_PREFIX>:runner:heartbeat`; the api reads
    the same key. A divergence (or a runner without the vars, falling back
    to defaults) makes health report `runner: down` while the runner's own
    healthcheck stays green."""
    api_env = config["services"]["api"]["environment"]
    runner_env = config["services"]["bot-runner"]["environment"]
    for name in ("CACHE_ENABLED", "CACHE_PREFIX"):
        assert name in api_env, f"api is missing {name}"
        assert name in runner_env, f"bot-runner is missing {name}"
        assert api_env[name] == runner_env[name], f"{name} differs between api and bot-runner"


def test_runner_env_has_no_inert_bot_knobs(config: dict[str, Any]) -> None:
    # CONSENT_ACK is hardcoded to true by the runner when it spawns bots;
    # carrying it in the runner env would advertise a knob that does nothing.
    assert "CONSENT_ACK" not in config["services"]["bot-runner"]["environment"]
