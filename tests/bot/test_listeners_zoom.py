"""Unit tests for the Zoom lifecycle detection loop.

The loop runs against scripted DOM snapshots and a fake recorder, so every
exit path executes in CI with no browser. Zero-valued timeouts make timing
deterministic: any elapsed real time already exceeds them.
"""

from pathlib import Path

import pytest
from bot.listeners import (
    EXIT_BOT_ERROR,
    EXIT_JOIN_TIMEOUT,
    EXIT_NEVER_ADMITTED,
    EXIT_OK,
    EXIT_REMOVED,
    BotOutcome,
    Timeouts,
)
from bot.listeners_zoom import _join_computer_audio, run_call_loop
from bot.record_audio import Recorder

from tests.bot.fakes import FakePage, ScriptedPage

FIXTURES = Path(__file__).parent / "fixtures" / "zoom"

LONG = Timeouts(
    waiting_room_s=9999.0,
    empty_room_s=9999.0,
    alone_grace_s=9999.0,
    max_record_s=9999.0,
)
INSTANT = Timeouts(
    waiting_room_s=0.0,
    empty_room_s=0.0,
    alone_grace_s=0.0,
    max_record_s=0.0,
)


@pytest.fixture(autouse=True)
def _instant_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fake-page tests fast: the real lookup timeouts would busy-wait
    seconds per admission with no browser behind the double.
    """
    monkeypatch.setattr("bot.listeners_zoom.AUDIO_DIALOG_TIMEOUT_MS", 0)
    monkeypatch.setattr("bot.listeners_zoom.MEDIA_MUTE_TIMEOUT_MS", 0)


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

    return run_call_loop(
        page,
        call_id="test-call",
        recorder=recorder,
        timeouts=timeouts,
        stop_requested=stop_requested,
    )


# --- admission -------------------------------------------------------------


def test_waiting_room_then_admitted_records_until_ended() -> None:
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


def test_never_admitted_from_waiting_room() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_waiting_room"), recorder, INSTANT)

    assert outcome.exit_code == EXIT_NEVER_ADMITTED
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert "waiting-room" in outcome.reason
    assert recorder.started is False


def test_host_not_started_until_deadline_has_distinct_reason() -> None:
    """Pre-admission the host-not-started screen waits like the waiting room
    (exit 2), but the terminal reason names it so the two are diagnosable.
    """
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_meeting_not_started"), recorder, INSTANT)

    assert outcome.exit_code == EXIT_NEVER_ADMITTED
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert "meeting-not-started" in outcome.reason
    assert recorder.started is False


def test_waiting_room_leave_control_never_reads_as_admission() -> None:
    """Live lesson from Meet (2026-09-09): the admission-wait page can
    render a leave control of its own. The waiting notice overrides it — no
    recording and no empty-room timer until true admission.
    """
    recorder = FakeRecorder()
    outcome = run(
        scripted(
            "zoom_waiting_room_with_leave",
            "zoom_waiting_room_with_leave",
            "zoom_waiting_room_with_leave",
        ),
        recorder,
        LONG,
        stop_after=2,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False
    assert recorder.stopped is False


def test_host_not_started_then_admitted_records() -> None:
    """The not-started screen must not be terminal once the host starts the
    meeting: the same poll loop admits and records.
    """
    recorder = FakeRecorder()
    outcome = run(
        scripted("zoom_meeting_not_started", "zoom_in_call", "zoom_in_call", "zoom_ended"),
        recorder,
        LONG,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert outcome.recording_started is True
    assert recorder.started is True


# --- recording phase -------------------------------------------------------


def test_removed_mid_recording_preserves_the_wav() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_in_call", "zoom_removed"), recorder, LONG)

    assert outcome.exit_code == EXIT_REMOVED
    assert outcome.end_reason == "removed"
    assert outcome.recording_started is True
    assert recorder.stopped is True


def test_call_ended_explicit() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_in_call", "zoom_ended"), recorder, LONG)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert recorder.stopped is True


def test_alone_after_others_left() -> None:
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=0.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(
        scripted("zoom_in_call", "zoom_in_call", "zoom_in_call_alone", "zoom_in_call_alone"),
        recorder,
        timeouts,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "alone"
    assert recorder.started is True
    assert recorder.stopped is True


def test_host_promotion_after_others_left_is_alone() -> None:
    """Live 2026-10-06: when the original host left, Zoom promoted the bot
    to host — Leave became End. That is still an in-call, alone state: the
    loop must exit 0 end_reason=alone after the grace, not treat the missing
    Leave control as a dead call.
    """
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=0.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(
        scripted(
            "zoom_in_call",
            "zoom_in_call",
            "zoom_in_call_host_alone",
            "zoom_in_call_host_alone",
        ),
        recorder,
        timeouts,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "alone"
    assert recorder.started is True
    assert recorder.stopped is True


def test_admitted_empty_room_is_exit_4() -> None:
    """Admission into a room that never had anyone else is the empty-room
    case (runner maps it to no_show because recording started), never the
    alone case: alone requires having seen two participants first.
    """
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=0.0,
        alone_grace_s=9999.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_in_call_alone", "zoom_in_call_alone"), recorder, timeouts)

    assert outcome.exit_code == EXIT_JOIN_TIMEOUT
    assert outcome.end_reason is None
    assert outcome.recording_started is True
    assert recorder.stopped is True


def test_unknown_participant_count_exits_5() -> None:
    """An unknown participant count is never inferred as empty: after the
    empty-room deadline the bot cannot verify the room and exits 5.
    """
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=0.0,
        alone_grace_s=9999.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_in_call_no_count", "zoom_in_call_no_count"), recorder, timeouts)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert recorder.stopped is True


def test_ambiguous_notice_mid_call_does_not_end_the_call() -> None:
    """Chat/notification copy that matches the removal/ended patterns must
    not end a live call: while the in-call leave control is present, the
    notice is ignored and recording continues (Meet's 2026-09-09 lesson).
    """
    recorder = FakeRecorder()
    outcome = run(
        scripted(
            "zoom_in_call",
            "zoom_in_call_notice_text",
            "zoom_in_call_notice_text",
            "zoom_in_call_notice_text",
            "zoom_in_call_notice_text",
        ),
        recorder,
        LONG,
        stop_after=5,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is True
    assert recorder.started is True


def test_missed_controls_confirmed_as_call_ended() -> None:
    """Unexplained disappearance of the in-call controls (no removal/ended
    notice) is confirmed over three polls before a clean call_ended.
    """
    recorder = FakeRecorder()
    outcome = run(
        scripted(
            "zoom_in_call",
            "zoom_in_call_controls_gone",
            "zoom_in_call_controls_gone",
            "zoom_in_call_controls_gone",
        ),
        recorder,
        LONG,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert "in-call indicators gone" in outcome.reason
    assert recorder.stopped is True


def test_max_record_duration_gives_up() -> None:
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=9999.0,
        max_record_s=0.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_in_call", "zoom_in_call"), recorder, timeouts)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "give_up"
    assert recorder.stopped is True


def test_recorder_death_is_exit_5() -> None:
    recorder = FakeRecorder(is_running_script=[False])
    outcome = run(scripted("zoom_in_call", "zoom_in_call"), recorder, LONG)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert recorder.stopped is True


def test_recorder_start_failure() -> None:
    recorder = FakeRecorder(fail_on_start=True)
    outcome = run(scripted("zoom_in_call"), recorder, LONG)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False


def test_stop_before_recording() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("zoom_waiting_room"), recorder, LONG, stop_after=2)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False


def test_stop_after_recording_finalizes() -> None:
    recorder = FakeRecorder()
    outcome = run(
        scripted("zoom_in_call", "zoom_in_call", "zoom_in_call"),
        recorder,
        LONG,
        stop_after=2,
    )

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is True
    assert recorder.started is True
    assert recorder.stopped is True


# --- post-admission audio --------------------------------------------------


def test_computer_audio_clicked_once_dialog_fixture() -> None:
    page = FakePage.from_fixture(FIXTURES / "zoom_audio_dialog.html")

    _join_computer_audio(page)

    assert "Join with computer audio" in page.clicked
    assert "Unmute my microphone" not in page.clicked


def test_computer_audio_attempted_once_after_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr("bot.listeners_zoom._join_computer_audio", lambda page: calls.append(1))
    recorder = FakeRecorder()
    outcome = run(
        scripted("zoom_in_call", "zoom_in_call", "zoom_in_call"),
        recorder,
        LONG,
        stop_after=2,
    )

    assert outcome.exit_code == EXIT_OK
    assert calls == [1]
