"""Tier-2 docker rehearsal for deploy/backup.sh + deploy/restore.sh.

Spins a throwaway compose project (postgres:17-alpine only) and drives the
real scripts against it: seed a `calls` row with a transcript, back it up,
restore into the throwaway database, and assert the transcript round-trips
while the live database is untouched. Also locks the two safety behaviors:
retention cleanup (old dumps deleted, the newest kept) and the refusal to
restore into reserved database names. Skips cleanly when no docker daemon
is reachable (mirrors tests/workers/test_bot_runner_docker.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "backup.sh"
RESTORE_SCRIPT = REPO_ROOT / "deploy" / "restore.sh"
THROWAWAY_DB = "oreeai_restore_test"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

SEED_SQL = """
CREATE TABLE calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transcript jsonb
);
INSERT INTO calls (transcript) VALUES
    ('[{"speaker": "S0", "text": "first speaker"}]'::jsonb),
    ('[{"speaker": "S1", "text": "second speaker"}]'::jsonb);
"""


def _docker(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Any:
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon not reachable")

    root = tmp_path_factory.mktemp("deploy-rehearsal")
    backups = root / "backups"
    backups.mkdir()
    project = f"oreeai-rehearsal-{uuid.uuid4().hex[:8]}"
    compose = root / "docker-compose.yml"
    compose.write_text(
        f"""name: {project}
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: oreeai
      POSTGRES_PASSWORD: rehearsal-password
      POSTGRES_DB: oreeai
    volumes:
      - {backups}:/backups
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U oreeai -d oreeai"]
      interval: 2s
      timeout: 3s
      retries: 30
"""
    )

    stack = {"root": root, "compose": compose, "backups": backups, "project": project}

    def compose_cmd(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", str(compose), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    started = compose_cmd("up", "-d", "--wait")
    assert started.returncode == 0, started.stderr
    seeded = compose_cmd(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "oreeai",
        "-d",
        "oreeai",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        SEED_SQL,
    )
    assert seeded.returncode == 0, seeded.stderr
    yield stack
    compose_cmd("down", "-v")


def psql(stack: dict[str, Any], sql: str, db: str = "oreeai") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(stack["compose"]),
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "oreeai",
            "-d",
            db,
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def script_env(stack: dict[str, Any]) -> dict[str, str]:
    """Hermetic env for the scripts; PATH + docker connectivity only."""
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
    env.update(
        {
            "COMPOSE_FILE": str(stack["compose"]),
            "BACKUP_DIR": str(stack["backups"]),
            "ENV_FILE": str(stack["root"] / "no-such.env"),
            "POSTGRES_USER": "oreeai",
            "POSTGRES_DB": "oreeai",
            "BACKUP_KEEP_DAYS": "30",
        }
    )
    return env


def run_script(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env, check=False
    )


def newest_dump(backups: Path) -> Path:
    dumps = sorted(backups.glob("oreeai-*.sql.gz"))
    assert dumps, "backup.sh produced no dump"
    return dumps[-1]


def test_backup_then_throwaway_restore_roundtrips(stack: dict[str, Any]) -> None:
    env = script_env(stack)
    backed_up = run_script(BACKUP_SCRIPT, env=env)
    assert backed_up.returncode == 0, backed_up.stderr
    dump = newest_dump(stack["backups"])
    assert "backup: OK" in backed_up.stdout

    restored = run_script(RESTORE_SCRIPT, str(dump), env=env)
    assert restored.returncode == 0, restored.stderr
    assert f"throwaway {THROWAWAY_DB} restored (2 calls)" in restored.stdout

    count = psql(stack, "SELECT count(*) FROM calls;", db=THROWAWAY_DB)
    assert count.stdout.strip() == "2"
    texts = psql(
        stack,
        "SELECT string_agg(t, ',' ORDER BY t) FROM ("
        "SELECT transcript->0->>'text' AS t FROM calls) AS texts;",
        db=THROWAWAY_DB,
    )
    assert texts.stdout.strip() == "first speaker,second speaker"


def test_live_database_is_untouched_by_the_rehearsal(stack: dict[str, Any]) -> None:
    # The roundtrip test restored into the throwaway database; the live
    # database must still hold exactly the seeded rows.
    count = psql(stack, "SELECT count(*) FROM calls;")
    assert count.stdout.strip() == "2"


def test_restore_refuses_reserved_targets(stack: dict[str, Any]) -> None:
    env = script_env(stack)
    dump = newest_dump(stack["backups"])
    for target in ("postgres", "oreeai"):
        refused = run_script(RESTORE_SCRIPT, str(dump), "--target", target, env=env)
        assert refused.returncode == 2, refused.stderr
        assert "refusing to restore into reserved database" in refused.stderr


def test_retention_cleanup_keeps_the_newest_dump(stack: dict[str, Any]) -> None:
    old = stack["backups"] / "oreeai-20000101T000000Z.sql.gz"
    old.write_bytes(b"old dump placeholder")
    forty_days_ago = time.time() - 40 * 86400
    os.utime(old, (forty_days_ago, forty_days_ago))

    result = run_script(BACKUP_SCRIPT, env=script_env(stack))
    assert result.returncode == 0, result.stderr
    assert not old.exists(), "dump older than BACKUP_KEEP_DAYS was not deleted"
    assert newest_dump(stack["backups"]).exists()


def test_newest_dump_is_a_valid_pg_dump_archive(stack: dict[str, Any]) -> None:
    dump = newest_dump(stack["backups"])
    assert dump.name.startswith("oreeai-")
    assert dump.name.endswith(".sql.gz")
    result = subprocess.run(
        ["bash", "-c", f"gzip -cd '{dump}' | head -n 5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "PostgreSQL database dump" in result.stdout
