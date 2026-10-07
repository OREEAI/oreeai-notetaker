"""Tier-2 docker tests for the platform dispatch and Zoom wiring (docker marker).

Mirrors the manual entrypoint scenarios that do not need a live Zoom
meeting: the real image boots the same Xvfb/PulseAudio graph and the
``BOT_PLATFORM`` guard picks the run module contract before any browser
work. Skips cleanly when no docker daemon is reachable or the bot image
has not been built (``make bot-build``); CI without the image skips too.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

IMAGE = "oreeai-bot:local"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]


def _docker(*argv: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=timeout
    )


@pytest.fixture(scope="session")
def docker_ready() -> None:
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon not reachable")


@pytest.fixture(scope="session")
def bot_image(docker_ready: None) -> str:
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.skip(f"{IMAGE} not built (run make bot-build)")
    return IMAGE


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    """bot logs go to stderr (stdlib logging); combine both streams."""
    return proc.stdout + proc.stderr


def test_entrypoint_rejects_unknown_platform(bot_image: str) -> None:
    """Unknown BOT_PLATFORM names the variable and exits 5 pre-boot."""
    proc = _docker("run", "--rm", "-e", "BOT_PLATFORM=banana", bot_image)

    assert proc.returncode == 5
    assert "BOT_PLATFORM" in proc.stderr
    assert "banana" in proc.stderr


def test_zoom_platform_dispatches_to_join_zoom(bot_image: str) -> None:
    """BOT_PLATFORM=zoom boots the graph and runs the Zoom module.

    Missing MEETING_URL exits the shared code 5 *after* the module logged
    its platform identity and emitted the shared result line, which proves
    the dispatch reached ``bot.join_zoom`` inside the real container.
    """
    proc = _docker("run", "--rm", "-e", "BOT_PLATFORM=zoom", "-e", "CONSENT_ACK=true", bot_image)
    output = _output(proc)

    assert proc.returncode == 5
    assert "platform=zoom" in output
    assert "MEETING_URL is required" in output
    assert "OREEAI_BOT_RESULT " in output
    payload = json.loads(output.split("OREEAI_BOT_RESULT ", 1)[1].splitlines()[0])
    assert payload == {"call_id": "spike", "end_reason": None, "exit_code": 5}


def test_zoom_fast_fail_on_unreachable_meeting(bot_image: str) -> None:
    """The real image runs the Zoom lifecycle module on a fast-failing URL.

    Navigation error -> shared exit 5 + result line, proving the Z2
    ``join_zoom -> listeners_zoom`` wiring imports and runs inside the
    container without a live meeting.
    """
    proc = _docker(
        "run",
        "--rm",
        "-e",
        "BOT_PLATFORM=zoom",
        "-e",
        "CONSENT_ACK=true",
        "-e",
        "MEETING_URL=https://127.0.0.1:9/",
        bot_image,
    )
    output = _output(proc)

    assert proc.returncode == 5
    assert "platform=zoom" in output
    assert "OREEAI_BOT_RESULT " in output
    payload = json.loads(output.split("OREEAI_BOT_RESULT ", 1)[1].splitlines()[0])
    assert payload == {"call_id": "spike", "end_reason": None, "exit_code": 5}
    assert "bot finished: exit_code=5" in output


def test_default_platform_still_runs_the_meet_module(bot_image: str) -> None:
    """No BOT_PLATFORM means Meet, byte-for-byte: consent gate first."""
    proc = _docker("run", "--rm", bot_image)
    output = _output(proc)

    assert proc.returncode == 6
    assert "auth_mode=anonymous" in output
    assert "OREEAI_BOT_RESULT " in output
    payload = json.loads(output.split("OREEAI_BOT_RESULT ", 1)[1].splitlines()[0])
    assert payload == {"call_id": "spike", "end_reason": None, "exit_code": 6}
