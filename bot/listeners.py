"""Meet call-state detection loop.

This module owns polling, timing, transitions, recording triggers, and
leaving. :mod:`bot.states` stays pure; :mod:`bot.join_meet` owns browser
startup/shutdown, configuration, and the process exit code.

Exit codes follow the Shared contracts table:

- ``0``: clean end. ``end_reason`` is one of ``call_ended``, ``removed`` is
  exit 3, ``alone``, or ``give_up``.
- ``2``: never admitted after ``BOT_WAITING_ROOM_TIMEOUT``.
- ``3``: removed mid-call.
- ``4``: empty-room timeout. The runner distinguishes ``join_timeout`` from
  ``no_show`` by whether the bot had started recording; the container only
  reports the timeout itself.
- ``5``: unexpected bot error.
- ``7``: silent recording. This code is selected by :mod:`bot.join_meet`
  after the silence check; this loop only returns the preceding clean ``0``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot import selectors, states
from bot.humanize import click_like_human, move_to

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from bot.record_audio import Recorder

logger = logging.getLogger("oreeai.bot.states")

EXIT_OK = 0
EXIT_NEVER_ADMITTED = 2
EXIT_REMOVED = 3
EXIT_JOIN_TIMEOUT = 4
EXIT_BOT_ERROR = 5
EXIT_SILENT_RECORDING = 7

POLL_INTERVAL_S = 2.0
ENDED_CONFIRMATION_POLLS = 3
FIRST_ADMISSION_EVIDENCE_S = 5.0
ADMISSION_EVIDENCE_INTERVAL_S = 30.0
UNKNOWN_ROOM_EVIDENCE_INTERVAL_S = 30.0
DEBUG_SCREENSHOT_NAME = "oreeai-debug"
_TEXT_SNIPPET_CHARS = 300

_FROM_JOIN_CLICKED = "join_clicked"
_WAITING_ROOM = "waiting_room"
_IN_CALL = "in_call"
_STOPPED = "stopped"
_BOT_ERROR = "bot_error"

_VALID_END_REASONS = ("call_ended", "removed", "alone", "give_up")


@dataclass(frozen=True)
class Timeouts:
    waiting_room_s: float = 600.0
    empty_room_s: float = 300.0
    alone_grace_s: float = 60.0
    max_record_s: float = 10800.0


@dataclass(frozen=True)
class BotOutcome:
    exit_code: int
    end_reason: str | None
    reason: str
    recording_started: bool = False

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("BotOutcome requires a reason")
        if self.end_reason == "removed":
            if self.exit_code != EXIT_REMOVED:
                raise ValueError("removed is only valid with the removal exit")
        elif self.end_reason is not None:
            if self.end_reason not in _VALID_END_REASONS:
                raise ValueError(f"invalid end_reason: {self.end_reason}")
            if self.exit_code != EXIT_OK:
                raise ValueError("end_reason is only valid with a clean exit")


def _now() -> float:
    return time.monotonic()


def _log_transition(call_id: str, from_state: str, to_state: str, reason: str) -> None:
    logger.info(
        "call_id=%s from_state=%s to_state=%s reason=%s",
        call_id,
        from_state,
        to_state,
        reason,
    )


def debug_screenshot(page: Page, reason: str) -> None:
    """Save Meet page evidence for a stalled or unexpected lifecycle state."""
    debug_dir = os.environ.get("DEBUG_DIR", "/tmp")
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = f"{debug_dir.rstrip('/')}/{DEBUG_SCREENSHOT_NAME}-{ts}.png"
    try:
        page.screenshot(path=path)
    except Exception:
        logger.exception("failed to save debug screenshot (%s)", reason)
        return
    try:
        body_text = page.evaluate("() => document.body && document.body.innerText || ''") or ""
    except Exception:
        body_text = ""
    snippet = " ".join(body_text.split())[:_TEXT_SNIPPET_CHARS]
    logger.warning(
        "state detection stalled (%s); url=%s; saved %s; text=%r",
        reason,
        page.url,
        path,
        snippet,
    )


def _leave_meet(page: Page) -> None:
    button = selectors.leave_call_button(page, timeout_ms=1000)
    if button is None:
        logger.warning("leave control absent; browser shutdown will end the Meet session")
        return
    move_to(page, button)
    click_like_human(button)
    logger.info("leave clicked")


def _finish_recording(
    page: Page,
    call_id: str,
    recorder: Recorder,
    phase: str,
    to_state: str,
    reason: str,
    exit_code: int,
    end_reason: str | None,
    *,
    leave_first: bool,
) -> BotOutcome:
    try:
        if leave_first:
            _leave_meet(page)
    finally:
        recorder.stop()
        logger.info("recording stopped")
    _log_transition(call_id, phase, to_state, reason)
    return BotOutcome(exit_code, end_reason, reason, recording_started=True)


def _handle_stop(
    page: Page,
    call_id: str,
    recorder: Recorder,
    phase: str,
    recording_started: bool,
) -> BotOutcome:
    reason = "stop requested"
    if not recording_started:
        _log_transition(call_id, phase, _STOPPED, reason)
        logger.info("stop requested before recording; leaving without recording")
        return BotOutcome(EXIT_OK, None, reason, recording_started=False)
    return _finish_recording(
        page,
        call_id,
        recorder,
        phase,
        _STOPPED,
        reason,
        EXIT_OK,
        None,
        leave_first=True,
    )


def run_call_loop(
    page: Page,
    *,
    call_id: str,
    recorder: Recorder,
    timeouts: Timeouts,
    stop_requested: Callable[[], bool],
    poll_interval_s: float = POLL_INTERVAL_S,
) -> BotOutcome:
    """Poll Meet state from join-click through a terminal lifecycle outcome."""
    start = _now()
    phase = _WAITING_ROOM
    _log_transition(call_id, _FROM_JOIN_CLICKED, phase, "join request submitted")

    admitted_at = 0.0
    recording_started = False
    saw_others = False
    alone_since: float | None = None
    unknown_since: float | None = None
    missed_leave = 0
    next_admission_evidence = start + FIRST_ADMISSION_EVIDENCE_S
    next_unknown_evidence = 0.0

    while True:
        if stop_requested():
            return _handle_stop(page, call_id, recorder, phase, recording_started)

        now = _now()
        removed, removed_detail = states.is_removed(page)
        ended, ended_detail = states.is_call_ended(page)
        admitted, admitted_detail = states.is_admitted(page)

        if not recording_started:
            if admitted:
                phase = _IN_CALL
                _log_transition(call_id, _WAITING_ROOM, phase, admitted_detail)
                try:
                    recorder.start()
                except Exception:
                    debug_screenshot(page, "recording failed to start")
                    _log_transition(call_id, phase, _BOT_ERROR, "recorder failed to start")
                    logger.exception("recorder failed to start")
                    reason = "recorder failed to start"
                    return BotOutcome(EXIT_BOT_ERROR, None, reason, recording_started=False)
                recording_started = True
                admitted_at = now
                unknown_since = now
                next_unknown_evidence = now + UNKNOWN_ROOM_EVIDENCE_INTERVAL_S
                logger.info("recording started")
                continue

            if selectors.join_blocked_indicator(page, timeout_ms=0) is not None:
                debug_screenshot(page, "meet blocked the join attempt")
                reason = "meet blocked the join attempt (anti-bot wall)"
                _log_transition(call_id, phase, _BOT_ERROR, reason)
                logger.error("%s", reason)
                return BotOutcome(EXIT_BOT_ERROR, None, reason, recording_started=False)

            if now - start >= timeouts.waiting_room_s:
                debug_screenshot(page, "never admitted")
                reason = f"waiting-room timeout after {timeouts.waiting_room_s} seconds"
                _log_transition(call_id, phase, "never_admitted", reason)
                logger.error("not admitted within %s seconds", timeouts.waiting_room_s)
                return BotOutcome(EXIT_NEVER_ADMITTED, None, reason, recording_started=False)

            if now >= next_admission_evidence:
                debug_screenshot(page, "waiting for admission (periodic evidence)")
                if selectors.knocking_indicator(page, timeout_ms=0) is not None:
                    logger.info("still knocking (host has not admitted the bot yet)")
                next_admission_evidence = now + ADMISSION_EVIDENCE_INTERVAL_S

            page.wait_for_timeout(int(poll_interval_s * 1000))
            continue

        if removed:
            try:
                recorder.stop()
                logger.info("recording stopped")
            finally:
                _log_transition(call_id, phase, "removed", removed_detail)
            logger.info("removed from call")
            return BotOutcome(EXIT_REMOVED, "removed", removed_detail, recording_started=True)

        if ended:
            try:
                recorder.stop()
                logger.info("recording stopped")
            finally:
                _log_transition(call_id, phase, "call_ended", ended_detail)
            logger.info("call ended")
            return BotOutcome(EXIT_OK, "call_ended", ended_detail, recording_started=True)

        if not recorder.is_running():
            debug_screenshot(page, "recorder process ended")
            reason = "parec died mid-recording; leaving with a truncated WAV"
            logger.error("%s", reason)
            return _finish_recording(
                page,
                call_id,
                recorder,
                phase,
                _BOT_ERROR,
                reason,
                EXIT_BOT_ERROR,
                None,
                leave_first=True,
            )

        if now - admitted_at >= timeouts.max_record_s:
            reason = f"max record duration reached after {timeouts.max_record_s} seconds"
            logger.info("%s", reason)
            return _finish_recording(
                page,
                call_id,
                recorder,
                phase,
                "gave_up",
                reason,
                EXIT_OK,
                "give_up",
                leave_first=True,
            )

        if admitted:
            missed_leave = 0
            count, count_detail = states.participant_count(page)
            alone, alone_detail = states.bot_alone_in_call(page)

            if count is not None and count >= 2:
                saw_others = True
                alone_since = None
                unknown_since = None
            elif alone:
                if saw_others:
                    if alone_since is None:
                        alone_since = now
                        logger.info("bot alone in call; starting alone grace (%s)", alone_detail)
                    elif now - alone_since >= timeouts.alone_grace_s:
                        reason = (
                            "bot alone after others left; "
                            f"alone grace of {timeouts.alone_grace_s} seconds elapsed"
                        )
                        return _finish_recording(
                            page,
                            call_id,
                            recorder,
                            phase,
                            "alone",
                            reason,
                            EXIT_OK,
                            "alone",
                            leave_first=True,
                        )
                else:
                    unknown_since = None
                    if now - admitted_at >= timeouts.empty_room_s:
                        reason = (
                            "room remained empty; "
                            f"empty-room timeout of {timeouts.empty_room_s} seconds elapsed"
                        )
                        logger.info("%s", reason)
                        return _finish_recording(
                            page,
                            call_id,
                            recorder,
                            phase,
                            "empty_room",
                            reason,
                            EXIT_JOIN_TIMEOUT,
                            None,
                            leave_first=True,
                        )
            elif saw_others:
                alone_since = None
                unknown_since = None
            else:
                if unknown_since is None:
                    unknown_since = now
                if now - unknown_since >= timeouts.empty_room_s:
                    debug_screenshot(page, "participant detection unavailable")
                    reason = (
                        "participant count unavailable; "
                        f"empty-room timeout of {timeouts.empty_room_s} seconds elapsed"
                    )
                    logger.error("%s", reason)
                    return _finish_recording(
                        page,
                        call_id,
                        recorder,
                        phase,
                        _BOT_ERROR,
                        reason,
                        EXIT_BOT_ERROR,
                        None,
                        leave_first=True,
                    )
                if now >= next_unknown_evidence:
                    debug_screenshot(page, "participant detection uncertain (periodic evidence)")
                    logger.info("participant count unavailable (%s)", count_detail)
                    next_unknown_evidence = now + UNKNOWN_ROOM_EVIDENCE_INTERVAL_S
        else:
            missed_leave += 1
            if missed_leave >= ENDED_CONFIRMATION_POLLS:
                debug_screenshot(page, "in-call controls gone")
                reason = f"in-call indicators gone for {missed_leave} polls; treating as call ended"
                try:
                    recorder.stop()
                    logger.info("recording stopped")
                finally:
                    _log_transition(call_id, phase, "call_ended", reason)
                logger.info("%s", reason)
                return BotOutcome(EXIT_OK, "call_ended", reason, recording_started=True)

        page.wait_for_timeout(int(poll_interval_s * 1000))
