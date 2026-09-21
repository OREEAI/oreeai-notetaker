"""Contracts for the committed deploy artifacts (no docker needed).

`deploy/env.example` must cover every variable the stack reads (the
ledger's env-var inventory), keep safe placeholders, and stay a single
source of truth (symlink to the root example). The shell scripts must be
syntactically valid — the docker-tier tests drive them for real, this is
the fast tier.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DEPLOY = REPO_ROOT / "deploy"
SCRIPTS = ("smoke.sh", "backup.sh", "restore.sh")

# Every variable in the ledger's env-var inventory that the app reads.
# (ASSEMBLYAI_API_KEY is conditional on the provider choice; the provider
# is settled on Deepgram and no such setting exists in config.py.)
INVENTORY_VARS = (
    "ENVIRONMENT",
    "DEBUG",
    "LOG_LEVEL",
    "PROJECT_NAME",
    "API_V1_PREFIX",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "DATABASE_URL",
    "REDIS_URL",
    "CACHE_ENABLED",
    "CACHE_PREFIX",
    "CACHE_TTL_SECONDS",
    "API_KEY",
    "CALL_CONCURRENCY_LIMIT",
    "BOT_IMAGE_TAG",
    "BOT_DOCKER_NETWORK",
    "AUDIO_HOST_PATH",
    "BOT_PROFILE",
    "BOT_MAX_RECORD_DURATION",
    "BOT_WAITING_ROOM_TIMEOUT",
    "BOT_EMPTY_ROOM_TIMEOUT",
    "BOT_ALONE_GRACE",
    "BOT_SILENCE_RMS_FLOOR",
    "BOT_AUTH_MODE",
    "BOT_PROFILE_DIR",
    "CONSENT_ACK",
    "WEBHOOK_HTTP_TIMEOUT",
    "WEBHOOK_MAX_ATTEMPTS",
    "WEBHOOK_TIMESTAMP_SKEW",
    "S3_ENDPOINT_URL",
    "S3_REGION",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_SSE",
    "S3_PRESIGN_TTL_SECONDS",
    "AUDIO_RETENTION_DAYS",
    "FAILED_AUDIO_RETENTION_DAYS",
    "TRANSCRIPTION_PROVIDER",
    "DEEPGRAM_API_KEY",
    "AUDIO_MAX_BYTES",
    "CORS_ORIGINS",
    "API_IMAGE_TAG",
    "API_HOST_PORT",
    "DOCKER_GID",
    "BACKUPS_HOST_PATH",
    "BACKUP_CRON",
    "BACKUP_KEEP_DAYS",
)


def parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_env_example_covers_the_inventory() -> None:
    values = parse_env_example()
    missing = [name for name in INVENTORY_VARS if name not in values]
    assert missing == [], f"deploy/env.example is missing: {missing}"


def test_env_example_has_placeholders_not_secrets() -> None:
    values = parse_env_example()
    assert "change-me" in values["API_KEY"]
    assert values["S3_SECRET_ACCESS_KEY"] == ""
    assert values["S3_ACCESS_KEY_ID"] == ""
    assert values["DEEPGRAM_API_KEY"] == ""
    assert values["S3_BUCKET"] == ""


def test_deploy_env_example_is_the_root_file() -> None:
    link = DEPLOY / "env.example"
    assert link.is_symlink()
    assert link.resolve() == ENV_EXAMPLE.resolve()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_deploy_scripts_are_syntax_valid() -> None:
    for script in SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY / script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_backup_cron_line_is_the_documented_schedule() -> None:
    cron = (DEPLOY / "backup.cron").read_text()
    assert "0 3 * * *" in cron
    assert "/opt/oreeai/deploy/backup.sh" in cron
    assert "/var/log/oreeai/backup.log" in cron
