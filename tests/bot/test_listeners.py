"""Unit tests for the lifecycle detection loop.

The loop runs against scripted DOM snapshots and a fake recorder, so every
exit path executes in CI with no browser. Zero-valued timeouts make timing
deterministic: any elapsed real time already exceeds them.
"""

from pathlib import Path

from bot.listeners import (
    EXIT_BOT_ERROR,
    EXIT_JOIN_TIMEOUT,
    EXIT_NEVER_ADMITTED,
    EXIT_OK,
    EXIT_REMOVED,
    BotOutcome,
    Timeouts,
    run_call_loop,
)
from bot.record_audio import Recorder

from tests.bot.fakes import ScriptedPage

FIXTURES = Path(__file__).parent / "fixtures"

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


def test_never_admitted() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("knocking"), recorder, INSTANT)

    assert outcome.exit_code == EXIT_NEVER_ADMITTED
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False


def test_removed_mid_call() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("in_call_three", "removed"), recorder, LONG)

    assert outcome.exit_code == EXIT_REMOVED
    assert outcome.end_reason == "removed"
    assert outcome.recording_started is True
    assert recorder.started is True
    assert recorder.stopped is True


def test_call_ended_explicit() -> None:
    recorder = FakeRecorder()
    outcome = run(scripted("in_call_three", "call_ended"), recorder, LONG)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert recorder.stopped is True


def test_call_ended_fallback() -> None:
    in_call = "in_call_three"
    gone = "waiting_room"
    recorder = FakeRecorder()
    outcome = run(scripted(in_call, gone, gone, gone), recorder, LONG)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "call_ended"
    assert recorder.stopped is True


def test_alone_grace() -> None:
    alone = "in_call_alone"
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=0.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted("in_call_three", "in_call_three", alone, alone), recorder, timeouts)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "alone"
    assert recorder.started is True
    assert recorder.stopped is True


def test_empty_room_timeout() -> None:
    alone = "in_call_alone"
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=0.0,
        alone_grace_s=9999.0,
        max_record_s=9999.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted(alone, alone), recorder, timeouts)

    assert outcome.exit_code == EXIT_JOIN_TIMEOUT
    assert outcome.end_reason is None
    assert recorder.stopped is True


def test_max_record_duration() -> None:
    timeouts = Timeouts(
        waiting_room_s=9999.0,
        empty_room_s=9999.0,
        alone_grace_s=9999.0,
        max_record_s=0.0,
    )
    recorder = FakeRecorder()
    outcome = run(scripted("in_call_three", "in_call_three"), recorder, timeouts)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason == "give_up"
    assert recorder.stopped is True


def test_recorder_death() -> None:
    recorder = FakeRecorder(is_running_script=[False])
    outcome = run(scripted("in_call_three", "in_call_three"), recorder, LONG)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert recorder.stopped is True


def test_stop_request() -> None:
    in_call = "in_call_three"
    recorder = FakeRecorder()
    outcome = run(scripted(in_call, in_call, in_call), recorder, LONG, stop_after=2)

    assert outcome.exit_code == EXIT_OK
    assert outcome.end_reason is None
    assert outcome.recording_started is True
    assert recorder.started is True
    assert recorder.stopped is True


def test_recorder_start_failure() -> None:
    recorder = FakeRecorder(fail_on_start=True)
    outcome = run(scripted("in_call_three"), recorder, LONG)

    assert outcome.exit_code == EXIT_BOT_ERROR
    assert outcome.end_reason is None
    assert outcome.recording_started is False
    assert recorder.started is False
