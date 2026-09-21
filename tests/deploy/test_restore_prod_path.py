"""No-daemon mirror of the `restore.sh --target-prod` decision branches.

The docker-tier test (`test_backup_restore_docker.py`) rehearses the
throwaway path against a real Postgres. The product path's three
outcomes are shell logic, so they are driven here with a `docker` shim
on PATH and the real `restore.sh` / `smoke.sh`:

- older/equal dump: `alembic upgrade head` succeeds, services start and
  are waited on for health, smoke must pass;
- revision unknown to the release: warn, leave the schema untouched,
  continue to start + smoke;
- any other migration error: fail closed, do NOT start the services,
  tell the operator the stack is left stopped;
- services never healthy within the timeout: fail with the health
  timeout message and a `ps` diagnostic.
"""

from __future__ import annotations

import gzip
import http.server
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE = REPO_ROOT / "deploy" / "restore.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")

SHIM = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"${SHIM_LOG}"
case "$*" in
  *"ps --status running"*) echo "shim-db-container"; exit 0 ;;
  *"exec -T db psql"*"-tAc"*) echo "2"; exit 0 ;;
  *"exec -T db psql"*"--quiet"*) cat >/dev/null; exit 0 ;;
  *"exec -T db psql"*) exit 0 ;;
  *"run --rm --no-deps api alembic upgrade head"*)
    case "${SHIM_ALEMBIC:-ok}" in
      ok) exit 0 ;;
      unknown)
        echo "ERROR [alembic.util.messaging] Can't locate revision identified by 'fakerev9999'" >&2
        exit 255
        ;;
      *)
        echo "sqlalchemy.exc.OperationalError: connection refused" >&2
        exit 1
        ;;
    esac
    ;;
  *"up -d --wait"*) exit "${SHIM_UP:-0}" ;;
  *) exit 0 ;;
esac
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        body = json.dumps(
            {
                "status": "ok",
                "components": {"database": "up", "cache": "up", "runner": "up"},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def health_server() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def prod_restore_env(tmp_path: Path) -> dict[str, str]:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "docker"
    shim.write_text(SHIM)
    shim.chmod(0o755)

    dump = tmp_path / "oreeai-20260101T000000Z.sql.gz"
    with gzip.open(dump, "wb") as handle:
        handle.write(b"--\n-- PostgreSQL database dump\n--\n")

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("name: shim\nservices: {}\n")

    env = {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SHIM_LOG": str(tmp_path / "shim.log"),
        "SHIM_ALEMBIC": "ok",
        "COMPOSE_FILE": str(compose),
        "ENV_FILE": os.devnull,
        "POSTGRES_USER": "oreeai",
        "POSTGRES_DB": "oreeai",
        "API_KEY": "restore-prod-test-key",
        "RESTORE_WAIT_TIMEOUT": "5",
        "RESTORE_DUMP": str(dump),
    }
    return env


def run_prod_restore(env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess[str]:
    process_env = dict(env)
    process_env.update(overrides)
    return subprocess.run(
        ["bash", str(RESTORE), process_env["RESTORE_DUMP"], "--target-prod", "--yes"],
        capture_output=True,
        text=True,
        env=process_env,
        check=False,
    )


def shim_log(env: dict[str, str]) -> list[str]:
    return Path(env["SHIM_LOG"]).read_text().splitlines()


def index_of(log: list[str], needle: str) -> int:
    for index, line in enumerate(log):
        if needle in line:
            return index
    return -1


def test_prod_restore_migrates_starts_healthy_and_smokes(
    prod_restore_env: dict[str, str],
) -> None:
    with health_server() as port:
        result = run_prod_restore(prod_restore_env, BASE_URL=f"http://127.0.0.1:{port}")
    assert result.returncode == 0, result.stderr
    assert "running migrations" in result.stdout
    assert "waiting for health" in result.stdout
    assert "smoke green" in result.stdout

    log = shim_log(prod_restore_env)
    migrate_at = index_of(log, "alembic upgrade head")
    wait_at = index_of(log, "up -d --wait")
    assert migrate_at >= 0, log
    assert wait_at > migrate_at, log


def test_prod_restore_warns_and_continues_on_unknown_revision(
    prod_restore_env: dict[str, str],
) -> None:
    with health_server() as port:
        result = run_prod_restore(
            prod_restore_env, SHIM_ALEMBIC="unknown", BASE_URL=f"http://127.0.0.1:{port}"
        )
    assert result.returncode == 0, result.stderr
    assert "revision is unknown to this release" in result.stderr
    assert "leaving the schema untouched" in result.stderr
    assert index_of(shim_log(prod_restore_env), "up -d --wait") >= 0


def test_prod_restore_fails_closed_on_other_migration_errors(
    prod_restore_env: dict[str, str],
) -> None:
    with health_server() as port:
        result = run_prod_restore(
            prod_restore_env, SHIM_ALEMBIC="error", BASE_URL=f"http://127.0.0.1:{port}"
        )
    assert result.returncode == 1
    assert "alembic upgrade head failed" in result.stderr
    assert "remain stopped" in result.stderr
    assert index_of(shim_log(prod_restore_env), "up -d --wait") == -1


def test_prod_restore_fails_when_services_never_become_healthy(
    prod_restore_env: dict[str, str],
) -> None:
    with health_server() as port:
        result = run_prod_restore(
            prod_restore_env, SHIM_UP="1", BASE_URL=f"http://127.0.0.1:{port}"
        )
    assert result.returncode == 1
    assert "did not become healthy" in result.stderr


def test_prod_restore_requires_yes(prod_restore_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(RESTORE), prod_restore_env["RESTORE_DUMP"], "--target-prod"],
        capture_output=True,
        text=True,
        env=prod_restore_env,
        check=False,
    )
    assert result.returncode == 2
    assert "--target-prod requires --yes" in result.stderr


def test_shim_fixture_sanity(prod_restore_env: dict[str, str]) -> None:
    # The shim itself must work before its branches can be trusted.
    result = subprocess.run(
        ["docker", "compose", "ps"],
        capture_output=True,
        text=True,
        env=prod_restore_env,
        check=False,
    )
    assert result.returncode == 0
    assert "compose ps" in Path(prod_restore_env["SHIM_LOG"]).read_text()
