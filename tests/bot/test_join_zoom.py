"""Unit tests for the Zoom join script.

Covers the pre-join flow (fixed name, mic/camera mute semantics), the
landing-page path into the web client, the fatal-wall detectors, the shared
``OREEAI_BOT_RESULT`` line, env parsing, and the entrypoint's
``BOT_PLATFORM`` dispatch. The lifecycle loop itself is covered by
``tests/bot/test_listeners_zoom.py``; this module only asserts the wiring.
All DOM comes from the fakes machinery; no browser or container is needed
in CI.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from bot.listeners import EXIT_BOT_ERROR, EXIT_OK, BotOutcome

from bot import join_zoom, selectors_zoom
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures" / "zoom"
ENTRYPOINT = Path(__file__).parents[2] / "bot" / "entrypoint.sh"


# --- pre-join flow ---------------------------------------------------------


def test_prejoin_types_fixed_name_and_mutes_mic_and_camera() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin.html")

    outcome = join_zoom._run_prejoin(page, "Oree Notetaker")

    assert outcome is None
    assert page.typed_text() == "Oree Notetaker"
    assert page.filled == ["Oree Notetaker"]
    assert "Mute" in page.clicked
    assert "Stop Video" in page.clicked


def test_prejoin_already_muted_skips_muting_clicks() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin_muted.html")

    outcome = join_zoom._run_prejoin(page, "Oree Notetaker")

    assert outcome is None
    assert "Unmute" not in page.clicked
    assert "Start Video" not in page.clicked


def test_prejoin_missing_mic_toggle_is_fatal() -> None:
    """A notetaker must never join with a live mic; a missing mute control
    is a hard fail (same rule as Meet's green room)."""
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin_no_mic.html")

    outcome = join_zoom._run_prejoin(page, "Oree Notetaker")

    assert outcome is not None
    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.reason == "microphone toggle not found"


def test_join_clicked_logs_direct(caplog: pytest.LogCaptureFixture) -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin.html")

    with caplog.at_level(logging.INFO, logger="oreeai.bot.zoom"):
        outcome = join_zoom._click_join(page)

    assert outcome is None
    assert "Join" in page.clicked
    # The pre-join form never transitions in fixtures, so the verified-click
    # fallback dispatches the DOM click (live behavior on the 2026-10 build).
    assert page.evaluated == ["el => el.click()"]
    assert any("join clicked (direct)" in record.getMessage() for record in caplog.records)


def test_missing_join_control_is_fatal() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin_no_join.html")

    outcome = join_zoom._click_join(page)

    assert outcome is not None
    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.reason == "join control never appeared"


# --- landing page / web-client path ---------------------------------------


def test_landing_browser_join_click() -> None:
    page = FakePage(
        (FIXTURES / "zoom_landing.html").read_text(encoding="utf-8"),
        url="https://zoom.us/j/1234567890",
    )

    outcome = join_zoom._open_web_client(page)

    assert outcome is None
    assert "Join from browser" in page.clicked
    # Live evidence: the trusted click does not navigate this build, so the
    # DOM-click fallback on the same control must fire (fixtures never
    # navigate, which is exactly the no-progress state).
    assert page.evaluated == ["el => el.click()"]


def test_landing_without_browser_path_is_fatal() -> None:
    page = FakePage(
        (FIXTURES / "zoom_landing_no_browser_link.html").read_text(encoding="utf-8"),
        url="https://zoom.us/j/1234567890",
    )

    outcome = join_zoom._open_web_client(page)

    assert outcome is not None
    assert outcome.exit_code == EXIT_BOT_ERROR
    assert "web-client join unavailable" in outcome.reason


def test_direct_wc_url_skips_the_landing_page() -> None:
    page = FakePage(
        (FIXTURES / "zoom_prejoin.html").read_text(encoding="utf-8"),
        url="https://us02web.zoom.us/wc/join/1234567890",
    )

    outcome = join_zoom._open_web_client(page)

    assert outcome is None
    assert page.clicked == []


@pytest.mark.parametrize(
    "fixture",
    [
        "zoom_sign_in_required.html",
        "zoom_only_authenticated.html",
        "zoom_desktop_app_required.html",
        "zoom_passcode.html",
    ],
)
def test_fatal_walls_map_to_exit_5(fixture: str) -> None:
    outcome = join_zoom._detect_fatal_wall(FakePage.from_fixture(FIXTURES / fixture))

    assert outcome is not None
    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.reason


# --- wiring ----------------------------------------------------------------


def test_spike_loop_is_absorbed_by_listeners_zoom() -> None:
    """Z2 replaced the Z1 spike loop with the full lifecycle module."""
    assert not hasattr(join_zoom, "run_zoom_spike_loop")
    assert callable(join_zoom.run_call_loop)


def test_announce_consent_uses_shared_policy_with_zoom_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z3 wiring: the hook stays on the shared consent policy (no fork),
    with Zoom's selector set for the chat controls."""
    calls: list[object] = []

    def fake_post(target: object, *, selector_set: object = None, **kwargs: object) -> bool:
        calls.append(selector_set)
        return True

    monkeypatch.setattr(join_zoom.consent, "post_chat_announcement", fake_post)

    join_zoom._announce_consent(FakePage.from_fixture(FIXTURES / "zoom_in_call.html"))

    assert calls == [selectors_zoom]


# --- env parsing and the shared result line --------------------------------


def test_timeouts_from_env_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "BOT_WAITING_ROOM_TIMEOUT",
        "BOT_EMPTY_ROOM_TIMEOUT",
        "BOT_ALONE_GRACE",
        "BOT_MAX_RECORD_DURATION",
    ):
        monkeypatch.delenv(var, raising=False)

    defaults = join_zoom._timeouts_from_env()
    assert defaults.waiting_room_s == 600.0
    assert defaults.max_record_s == 10800.0

    monkeypatch.setenv("BOT_WAITING_ROOM_TIMEOUT", "42.5")
    monkeypatch.setenv("BOT_MAX_RECORD_DURATION", "abc")
    overridden = join_zoom._timeouts_from_env()
    assert overridden.waiting_room_s == 42.5
    assert overridden.max_record_s == 10800.0


def test_silence_floor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_SILENCE_RMS_FLOOR", "77")
    assert join_zoom._silence_floor_from_env() == 77.0


def test_result_line_emission(caplog: pytest.LogCaptureFixture) -> None:
    outcome = BotOutcome(EXIT_OK, "call_ended", "ended")

    with caplog.at_level(logging.INFO, logger="oreeai.bot.result"):
        join_zoom._emit_result("cid-1", outcome, EXIT_OK)

    payloads = [
        record.getMessage() for record in caplog.records if record.name == "oreeai.bot.result"
    ]
    assert payloads
    raw = payloads[-1].removeprefix("OREEAI_BOT_RESULT ")
    assert raw
    assert json.loads(raw) == {"call_id": "cid-1", "end_reason": "call_ended", "exit_code": EXIT_OK}


# --- entrypoint dispatch ---------------------------------------------------


def _run_entrypoint(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    env.update(overrides)
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_unknown_bot_platform_fails_fast() -> None:
    """Unknown BOT_PLATFORM exits 5 before any Xvfb/audio/browser work."""
    proc = _run_entrypoint({"BOT_PLATFORM": "banana"})

    assert proc.returncode == 5
    assert "BOT_PLATFORM" in proc.stderr
    assert "banana" in proc.stderr


def test_zoom_login_mode_fails_fast_until_z3() -> None:
    proc = _run_entrypoint({"BOT_PLATFORM": "zoom", "BOT_ENTRY_MODE": "login"})

    assert proc.returncode == 5
    assert "login" in proc.stderr.lower()
