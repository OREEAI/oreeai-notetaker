"""Unit tests for deploy/smoke.sh (no docker needed).

`smoke.sh` is the deploy gate an operator runs on the VPS, so its exit
codes are a contract: 0 healthy, 2 missing configuration, 3 HTTP failure,
4 a component reported not-up. These tests drive the real script against
an in-process HTTP server, including the quoted-`.env` handling that must
match compose's dotenv semantics.
"""

from __future__ import annotations

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
SMOKE = REPO_ROOT / "deploy" / "smoke.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    payload = b"{}"
    received: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        type(self).received = {key.lower(): value for key, value in self.headers.items()}
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def health_server(status: int, body: dict[str, Any] | None = None) -> Iterator[int]:
    _Handler.status = status
    _Handler.payload = json.dumps(body if body is not None else {}).encode()
    _Handler.received = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def health_body(database: str = "up", cache: str = "up", runner: str = "up") -> dict[str, Any]:
    return {
        "status": "ok",
        "components": {"database": database, "cache": cache, "runner": runner},
    }


def run_smoke(port: int, **env: str) -> subprocess.CompletedProcess[str]:
    process_env = {"PATH": os.environ.get("PATH", "")}
    process_env.update(
        {
            "BASE_URL": f"http://127.0.0.1:{port}",
            "ENV_FILE": os.devnull,
            "API_KEY": "smoke-test-key",
        }
    )
    process_env.update(env)
    return subprocess.run(
        ["bash", str(SMOKE)], capture_output=True, text=True, env=process_env, check=False
    )


def test_healthy_stack_exits_zero_and_sends_api_key() -> None:
    with health_server(200, health_body()) as port:
        result = run_smoke(port)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout
    assert _Handler.received["x-api-key"] == "smoke-test-key"


def test_non_200_exits_three() -> None:
    with health_server(503, health_body(runner="down")) as port:
        result = run_smoke(port)
    assert result.returncode == 3
    assert "HTTP 503" in result.stderr


def test_wrong_key_401_exits_three() -> None:
    with health_server(401, {"detail": "Invalid or missing API key"}) as port:
        result = run_smoke(port)
    assert result.returncode == 3
    assert "HTTP 401" in result.stderr


def test_component_not_up_exits_four_even_on_200() -> None:
    # A 200 can carry `cache: down` when the runner is merely `unknown`.
    with health_server(200, health_body(cache="down", runner="unknown")) as port:
        result = run_smoke(port)
    assert result.returncode == 4
    assert "cache is down" in result.stderr


def test_missing_api_key_exits_two() -> None:
    with health_server(200, health_body()) as port:
        result = run_smoke(port, API_KEY="")
    assert result.returncode == 2
    assert "API_KEY" in result.stderr


def test_quoted_env_values_are_unquoted_like_compose(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('API_KEY="quoted-key-from-dotenv"\n')
    with health_server(200, health_body()) as port:
        result = run_smoke(port, ENV_FILE=str(env_file), API_KEY="")
    assert result.returncode == 0, result.stderr
    assert _Handler.received["x-api-key"] == "quoted-key-from-dotenv"
