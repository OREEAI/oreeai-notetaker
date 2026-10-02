"""Unit tests for the Zoom join script (Z1 spike).

Covers the pre-join flow (fixed name, mic/camera mute semantics), the spike
loop's exit mapping, the shared ``OREEAI_BOT_RESULT`` line, env parsing, and
the entrypoint's ``BOT_PLATFORM`` dispatch. All DOM comes from the fakes
machinery; no browser or container is needed in CI.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from bot.listeners import (
    EXIT_BOT_ERROR,
    EXIT_NEVER_ADMITTED,
    EXIT_OK,
    EXIT_REMOVED,
    BotOutcome,
    Timeouts,
)
from bot.record_audio import Recorder

from bot import join_zoom
from tests.bot.fakes import FakePage, ScriptedPage

FIXTURES = Path(__file__).parent / "fixtures" / "zoom"
ENTRYPOINT = Path(__file__).parents[2] / "bot" / "entrypoint.sh"

LONG = Timeouts(
    waiting_room_s=9999.0,
    empty_room_s=9999.0,
    alone_grace_s=9999.0,
    max_record_s=9999.0,
)
INSTANT_WAITING = Timeouts(
    waiting_room_s=0.0,
    empty_room_s=9999.0,
    alone_grace_s=9999.0,
    max_record_s=9999.0,
)


class FakeRecorder(Recorder):
    """Recorder double: never spawns parec."""

    def __init__(
        self, is_running_script: list[bool] | None = None, fail_on_start: bool = False
    ) -> None:
        super().__init__("/tmp/fake-recording.wav")
        self.started = False
        self.stopped = False
        self.fail_on_start = fail_on_start
        self._script = list(is_running_script or [])

    def start(self) -> None:
        if self.fail_on_start:
            raise RuntimeError("parec exited immediately: fake failure")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def is_running(self) -> bool:
        if self._script:
            return self._script.pop(0)
        return self.started and not self.stopped


def scripted(*names: str) -> ScriptedPage:
    return ScriptedPage([FIXTURES / f"{name}.html" for name in names])


def run(
    page: ScriptedPage,
    recorder: FakeRecorder,
    timeouts: Timeouts,
    stop_after: int | None = None,
) -> BotOutcome:
    calls = 0

    def stop_requested() -> bool:
        nonlocal calls
        calls += 1
        return stop_after is not None and calls > stop_after

    return join_zoom.run_zoom_spike_loop(
        page,
        call_id="test-call",
        recorder=recorder,
        timeouts=timeouts,
        stop_requested=stop_requested,
    )


# --- pre-join flow ---------------------------------------------------------


def test_prejoin_types_fixed_name_and_mutes_mic_and_camera() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin.html")

    outcome = join_zoom._run_prejoin(page, "Oree Notetaker")

    assert outcome is None
    assert page.typed_text() == "Oree Notetaker"
    assert "Mute my microphone" in page.clicked
    assert "Stop my video" in page.clicked


def test_prejoin_already_muted_skips_muting_clicks() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_prejoin_muted.html")

    outcome = join_zoom._run_prejoin(page, "Oree Notetaker")

    assert outcome is None
    assert "Unmute my microphone" not in page.clicked
    assert "Start my video" not in page.clicked


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
    assert "Join from your browser" in page.clicked


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


# --- spike loop exit mapping ----------------------------------------------


def test_spike_loop_admits_records_and_ends() -> None:
    recorder = FakeRecorder()

    outcome = run(
        scripted("zoom_waiting_room", "zoom_in_call", "zoom_in_call", "zoom_ended"),
        recorder,
        LONG,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert outcome.recording_started is True
    assert recorder.started is True
    assert recorder.stopped is True


def test_spike_loop_never_admitted() -> None:
    recorder = FakeRecorder()

    outcome = run(scripted("zoom_waiting_room"), recorder, INSTANT_WAITING)

    assert outcome.exit_code == EXIT_NEVER_ADMITTED
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False


def test_spike_loop_removed_mid_call() -> None:
    recorder = FakeRecorder()

    outcome = run(scripted("zoom_in_call", "zoom_removed"), recorder, LONG)

    assert outcome.exit_code == EXIT_REMOVED
    assert outcome.end_reason == "removed"
    assert outcome.recording_started is True
    assert recorder.stopped is True


def test_spike_loop_give_up_on_max_duration() -> None:
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=9999.0,
        max_record_s=0.0,
    )
    recorder = FakeRecorder()

    outcome = run(scripted("zoom_in_call"), recorder, timeouts)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "give_up"
    assert recorder.stopped is True


def test_spike_loop_recorder_death_is_exit_5() -> None:
    recorder = FakeRecorder(is_running_script=[False])

    outcome = run(scripted("zoom_in_call", "zoom_in_call"), recorder, LONG)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert recorder.stopped is True


def test_spike_loop_stop_before_recording() -> None:
    recorder = FakeRecorder()

    outcome = run(scripted("zoom_waiting_room"), recorder, LONG, stop_after=2)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False


def test_spike_loop_stop_after_recording() -> None:
    recorder = FakeRecorder()

    outcome = run(scripted("zoom_in_call"), recorder, LONG, stop_after=3)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is True
    assert recorder.stopped is True


# --- audio graph hook ------------------------------------------------------


def test_join_computer_audio_clicks_dialog_and_leaves_muted_mic_alone() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_audio_dialog.html")

    join_zoom._join_computer_audio(page)

    assert "Join with computer audio" in page.clicked
    assert "Unmute my microphone" not in page.clicked


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
