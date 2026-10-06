"""Zoom web-client call-state detection loop.

This module owns polling, timing, transitions, recording triggers, and
leaving for Zoom sessions. :mod:`bot.states_zoom` stays pure;
:mod:`bot.join_zoom` owns browser startup/shutdown, the pre-join flow,
configuration, the silence check, and the process exit code.

Exit codes follow the table in bot/README.md:

- ``0``: clean end. ``end_reason`` is one of ``call_ended``, ``alone``, or
  ``give_up``; ``removed`` is exit 3.
- ``2``: never admitted after ``BOT_WAITING_ROOM_TIMEOUT``. Both the waiting
  room and the host-not-started screen keep the bot waiting; the
  host-not-started terminal logs a distinct reason string.
- ``3``: removed mid-call; the WAV recorded before removal is preserved.
- ``4``: admitted into an empty/unstarted room past ``BOT_EMPTY_ROOM_TIMEOUT``.
  The runner distinguishes ``join_timeout`` from ``no_show`` by whether the
  bot had started recording; the container only reports the timeout itself.
- ``5``: unexpected bot error (wall during admission, dead recorder,
  unavailable room detection).
- ``6``: consent refused. :mod:`bot.join_zoom` checks ``CONSENT_ACK`` before
  any browser work; this loop never returns it.
- ``7``: silent recording. This code is selected by :mod:`bot.join_zoom`
  after the silence check; this loop only returns the preceding clean ``0``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from bot import selectors_zoom, states_zoom
from bot.humanize import click_like_human, move_to, pause_between_actions
from bot.join_meet import MEDIA_MUTE_TIMEOUT_MS
from bot.listeners import (
    ADMISSION_EVIDENCE_INTERVAL_S,
    ENDED_CONFIRMATION_POLLS,
    EXIT_BOT_ERROR,
    EXIT_JOIN_TIMEOUT,
    EXIT_NEVER_ADMITTED,
    EXIT_OK,
    EXIT_REMOVED,
    FIRST_ADMISSION_EVIDENCE_S,
    POLL_INTERVAL_S,
    UNKNOWN_ROOM_EVIDENCE_INTERVAL_S,
    BotOutcome,
    Timeouts,
    debug_screenshot,
)

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

    from bot.record_audio import Recorder

logger = logging.getLogger("oreeai.bot.zoom.states")

# Budget for the post-admission computer-audio dialog; the live account
# auto-joins computer audio, so this only fires on configurations that show
# the dialog.
AUDIO_DIALOG_TIMEOUT_MS = 8000

_FROM_JOIN_CLICKED = "join_clicked"
_WAITING_ROOM = "waiting_room"
_IN_CALL = "in_call"
_STOPPED = "stopped"
_BOT_ERROR = "bot_error"

# Zoom's media controls are labelled with the action they will perform
# ("Unmute" when muted; "Start Video" when off). The pre-join mic label is
# static in the live build (state lives in the icon), so these markers only
# catch explicit already-off labels.
_MIC_ALREADY_OFF = ("unmute",)
_CAMERA_ALREADY_OFF = ("start video", "turn on camera", "start my video")


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


def _mute_media(
    page: Page,
    toggle: Callable[..., Locator | None],
    label: str,
    *,
    required: bool = False,
    already_off_markers: tuple[str, ...],
) -> bool:
    """Mute a Zoom media toggle. Returns True when muted or already off.

    When required and the toggle is not found, returns False so the caller
    can refuse to join: a notetaker must never join with a live mic (the
    container mic hears the call's own output, so an open mic echoes).
    """
    locator = toggle(page, timeout_ms=MEDIA_MUTE_TIMEOUT_MS)
    if locator is None:
        if required:
            logger.error("%s toggle not found; refusing to join unmuted (echo risk)", label)
            return False
        logger.warning("%s toggle not found; continuing without muting", label)
        return True
    aria = (locator.get_attribute("aria-label") or "").lower()
    if any(marker in aria for marker in already_off_markers):
        logger.info("%s already off", label)
        return True
    try:
        move_to(page, locator)
        click_like_human(locator)
    except Exception:
        if required:
            logger.error("%s toggle click failed; refusing to join unmuted (echo risk)", label)
            return False
        logger.warning("%s toggle click failed; continuing without muting", label)
        return True
    logger.info("%s muted before joining", label)
    return True


def _detect_fatal_wall(page: Page) -> BotOutcome | None:
    """Return an exit-5 outcome when a Zoom wall makes the join impossible.

    These are the queue contract's fast fails: the same screens a human in
    incognito would be stopped by. Never guessed at, never retried.
    """
    checks: tuple[tuple[Callable[[Page], tuple[bool, str]], str], ...] = (
        (
            states_zoom.is_sign_in_required,
            "guest join hit Zoom's sign-in-required wall (invisible CAPTCHA); "
            "Z3's signed-in mode is the fallback",
        ),
        (
            states_zoom.is_only_authenticated,
            "meeting allows only authenticated users",
        ),
        (
            states_zoom.is_desktop_app_required,
            "meeting requires the Zoom desktop app or has E2EE on; no web-client path",
        ),
        (
            states_zoom.is_passcode_screen,
            "meeting passcode missing or rejected; put ?pwd= in the meeting URL",
        ),
    )
    for predicate, reason in checks:
        detected, detail = predicate(page)
        if detected:
            debug_screenshot(page, reason)
            logger.error("%s (%s)", reason, detail)
            return BotOutcome(EXIT_BOT_ERROR, None, reason)
    return None


def _join_computer_audio(page: Page) -> None:
    """Join the meeting audio so the call plays into the virtual speaker.

    Without this the web client stays audio-muted and the WAV records
    silence. Best-effort: a missing dialog is logged with evidence and the
    loop continues (the silence floor will catch a mute-stuck capture on a
    clean exit).
    """
    button = selectors_zoom.audio_join_button(page, timeout_ms=AUDIO_DIALOG_TIMEOUT_MS)
    if button is None:
        debug_screenshot(page, "computer-audio join control not found")
        logger.warning("computer-audio control not found; meeting audio may stay muted")
    else:
        pause_between_actions(page)
        move_to(page, button)
        click_like_human(button)
        logger.info("joined computer audio")
    # Joining computer audio can reset the mic state; re-mute best-effort.
    _mute_media(
        page,
        selectors_zoom.microphone_toggle,
        "microphone",
        required=False,
        already_off_markers=_MIC_ALREADY_OFF,
    )


def _leave_zoom(page: Page) -> None:
    button = selectors_zoom.leave_button(page, timeout_ms=1000)
    if button is None:
        logger.warning("leave control absent; browser shutdown will end the Zoom session")
        return
    try:
        move_to(page, button)
        click_like_human(button)
        logger.info("leave clicked")
    except Exception:
        logger.warning("leave control click failed; browser shutdown will end the Zoom session")


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
            _leave_zoom(page)
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
    """Poll Zoom state from join-click through a terminal lifecycle outcome.

    Admission must be evidenced by Zoom's in-call anchors (the leave control
    with no waiting/not-started notice); the waiting-room and
    host-not-started screens both keep waiting until
    ``BOT_WAITING_ROOM_TIMEOUT``. While recording, removal/ended notices are
    trusted only once the in-call anchor is gone: the same text can ride in
    on chat or notification copy, and a live call must never be ended by it.
    """
    start = _now()
    phase = _WAITING_ROOM
    _log_transition(call_id, _FROM_JOIN_CLICKED, phase, "join request submitted")

    admitted_at = 0.0
    recording_started = False
    saw_others = False
    alone_since: float | None = None
    unknown_since: float | None = None
    missed_controls = 0
    next_admission_evidence = start + FIRST_ADMISSION_EVIDENCE_S
    next_unknown_evidence = 0.0

    while True:
        if stop_requested():
            return _handle_stop(page, call_id, recorder, phase, recording_started)

        now = _now()
        removed, removed_detail = states_zoom.is_removed(page)
        ended, ended_detail = states_zoom.is_call_ended(page)
        admitted, admitted_detail = states_zoom.is_admitted(page)

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
                try:
                    _join_computer_audio(page)
                except Exception:
                    logger.exception("joining computer audio failed; continuing")
                continue

            wall = _detect_fatal_wall(page)
            if wall is not None:
                _log_transition(call_id, phase, _BOT_ERROR, wall.reason)
                return wall

            if now - start >= timeouts.waiting_room_s:
                debug_screenshot(page, "never admitted")
                not_started, _ = states_zoom.is_meeting_not_started(page)
                if not_started:
                    reason = f"meeting-not-started timeout after {timeouts.waiting_room_s} seconds"
                else:
                    reason = f"waiting-room timeout after {timeouts.waiting_room_s} seconds"
                _log_transition(call_id, phase, "never_admitted", reason)
                logger.error("%s", reason)
                return BotOutcome(EXIT_NEVER_ADMITTED, None, reason, recording_started=False)

            if now >= next_admission_evidence:
                waiting, waiting_detail = states_zoom.is_in_waiting_room(page)
                not_started, not_started_detail = states_zoom.is_meeting_not_started(page)
                detail = (
                    waiting_detail
                    if waiting
                    else not_started_detail
                    if not_started
                    else "no waiting-room or not-started notice visible"
                )
                debug_screenshot(page, f"waiting for admission ({detail})")
                logger.info("still waiting for admission (%s)", detail)
                next_admission_evidence = now + ADMISSION_EVIDENCE_INTERVAL_S

            page.wait_for_timeout(int(poll_interval_s * 1000))
            continue

        # The in-call toolbar (Leave, or End once the bot is host) is the
        # admission anchor: a removal/ended notice while it is present is
        # chat or notification copy, and must not end a live call. The
        # notice is honored only once the anchor is gone (the real terminal
        # screens replace the toolbar).
        anchor, anchor_detail = states_zoom.in_call_controls(page)

        if removed and not anchor:
            try:
                recorder.stop()
                logger.info("recording stopped")
            finally:
                _log_transition(call_id, phase, "removed", removed_detail)
            logger.info("removed from meeting")
            return BotOutcome(EXIT_REMOVED, "removed", removed_detail, recording_started=True)

        if ended and not anchor:
            try:
                recorder.stop()
                logger.info("recording stopped")
            finally:
                _log_transition(call_id, phase, "call_ended", ended_detail)
            logger.info("call ended")
            return BotOutcome(EXIT_OK, "call_ended", ended_detail, recording_started=True)

        if removed or ended:
            logger.warning(
                "removal/ended notice text visible mid-call while the in-call controls "
                "are present (%s); staying in the call",
                anchor_detail,
            )

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

        if anchor:
            missed_controls = 0
            count, count_detail = states_zoom.participant_count(page)
            alone, alone_detail = states_zoom.bot_alone_in_call(page)

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
                    logger.info("%s", count_detail)
                    next_unknown_evidence = now + UNKNOWN_ROOM_EVIDENCE_INTERVAL_S
        else:
            # Admitted previously, but the in-call controls are gone and no
            # removal/ended notice explains it. Confirm over three polls
            # before treating it as the call ending (Meet's counter).
            missed_controls += 1
            if missed_controls >= ENDED_CONFIRMATION_POLLS:
                debug_screenshot(page, "in-call controls gone")
                reason = (
                    f"in-call indicators gone for {missed_controls} polls; treating as call ended"
                )
                try:
                    recorder.stop()
                    logger.info("recording stopped")
                finally:
                    _log_transition(call_id, phase, "call_ended", reason)
                logger.info("%s", reason)
                return BotOutcome(EXIT_OK, "call_ended", reason, recording_started=True)

        page.wait_for_timeout(int(poll_interval_s * 1000))
