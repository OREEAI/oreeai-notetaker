"""Join a Zoom meeting through the web client as a guest and record its audio.

Z1 spike (gate): proves a guest web-client bot can hear a real Zoom call.
The lifecycle here is deliberately minimal — join → admitted → recorder →
ended/removed/timeout — and is superseded by Z2's ``bot/listeners_zoom.py``
when the full Zoom lifecycle lands. Exit codes follow the shared table in
``bot/README.md``; this module never invents new ones.

Everything platform-independent is reused verbatim from the Meet modules:
the browser launch config and anonymous context (``launch_browser`` /
``_launch_anonymous`` in :mod:`bot.join_meet`) so the probe cannot drift,
the stealth pass and humanized input (``bot.stealth``, ``bot.humanize``),
the consent policy (``bot.consent``), the recorder with its pinned WAV
format (:class:`bot.record_audio.Recorder`), and the exit-code/result-line
helpers from :mod:`bot.join_meet` / :mod:`bot.listeners`.

    Env vars:
    MEETING_URL   (required) the https://zoom.us/j/<id> (+ ?pwd=) or direct
                  https://<org>.zoom.us/wc/join/<id> link to join
    CONSENT_ACK   (required) must be true; without it the bot exits 6 before
                  any browser work. Consent lives in bot/consent.py.
    BOT_NAME      DEPRECATED: the name is hard-coded to "Oree Notetaker".
                  Any other value logs a deprecation warning naming both
                  values, then the hard-coded name is used.
    CALL_ID       WAV file name, default "spike" (runner passes the real id)
    LOG_LEVEL     stdlib level name, default INFO
    DEBUG_DIR     where to write /tmp/oreeai-debug-<ts>.png on state stall;
                  default /tmp (local runs set /debug via the Makefile)
    BOT_WAITING_ROOM_TIMEOUT
                  seconds waiting for admission before exit 2, default 600
    BOT_MAX_RECORD_DURATION
                  maximum recording seconds before a clean exit 0, default 10800
    BOT_SILENCE_RMS_FLOOR
                  whole-WAV RMS floor, in raw 16-bit units, default 50
    PAREC_DEVICE  diagnostic PulseAudio-device override for parec; default is
                  the operational virtual-speaker monitor
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright.sync_api import sync_playwright

from bot import consent, selectors_zoom, states_zoom
from bot.humanize import (
    click_like_human,
    dwell_before_start,
    move_to,
    pause_between_actions,
    type_text,
)
from bot.join_meet import (
    GOTO_TIMEOUT_MS,
    JOIN_BUTTON_TIMEOUT_S,
    MEDIA_MUTE_TIMEOUT_MS,
    _configure_logging,
    _emit_result,
    _finalize_recording,
    _launch_anonymous,
    _silence_floor_from_env,
    _Stop,
    _timeouts_from_env,
    _wav_path,
)
from bot.listeners import (
    ADMISSION_EVIDENCE_INTERVAL_S,
    EXIT_BOT_ERROR,
    EXIT_CONSENT_MISSING,
    EXIT_NEVER_ADMITTED,
    EXIT_OK,
    EXIT_REMOVED,
    FIRST_ADMISSION_EVIDENCE_S,
    BotOutcome,
    Timeouts,
    debug_screenshot,
)
from bot.record_audio import Recorder

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = logging.getLogger("oreeai.bot.zoom")

# Budgets for the Zoom pre-join flow. The landing page can take a moment to
# render after navigation; the web-client form follows the click-through.
BROWSER_JOIN_TIMEOUT_MS = 15000
PREJOIN_READY_TIMEOUT_S = 20.0
PREJOIN_READY_TIMEOUT_MS = int(PREJOIN_READY_TIMEOUT_S * 1000)
JOIN_BUTTON_TIMEOUT_MS = int(JOIN_BUTTON_TIMEOUT_S * 1000)
AUDIO_DIALOG_TIMEOUT_MS = 8000
POLL_INTERVAL_S = 2.0

_FROM_JOIN_CLICKED = "join_clicked"
_WAITING_ROOM = "waiting_room"
_IN_CALL = "in_call"
_STOPPED = "stopped"
_BOT_ERROR = "bot_error"

# Zoom's media controls are labelled with the action they will perform
# ("Mute" when live, "Unmute" when muted; "Stop Video" / "Start Video"). The
# pre-join mic label is static in the live build (state lives in the icon),
# so these markers only catch explicit already-off labels.
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


def _open_web_client(page: Page) -> BotOutcome | None:
    """Take the human path from the /j landing page into the web client.

    Returns an error outcome when no browser join path exists; None when the
    web-client page is (or already was) the current page.

    Live evidence (2026-10-05): the landing control is a button labelled
    "Join from browser"; Playwright's trusted click fires the button's
    handler but does not always navigate (the same control's DOM click does),
    so a verified fallback dispatches ``el.click()`` when no progress is
    visible. This never bypasses a wall — walls are detected separately.
    """
    if "/wc/" in page.url:
        logger.info("direct web-client URL; skipping the landing page")
        return None

    accept = selectors_zoom.cookie_accept_button(page, timeout_ms=1500)
    if accept is not None:
        try:
            move_to(page, accept)
            click_like_human(accept)
            logger.info("cookie banner accepted")
        except Exception:
            logger.warning("cookie banner click failed; continuing")

    link = selectors_zoom.browser_join_link(page, timeout_ms=BROWSER_JOIN_TIMEOUT_MS)
    if link is None:
        wall = _detect_fatal_wall(page)
        if wall is not None:
            return wall
        debug_screenshot(page, "no 'join from your browser' path")
        reason = (
            "web-client join unavailable (host disabled 'Join from your browser' or E2EE is on)"
        )
        logger.error("%s", reason)
        return BotOutcome(EXIT_BOT_ERROR, None, reason)
    pause_between_actions(page)
    move_to(page, link)
    click_like_human(link)
    logger.info("join from your browser clicked")
    page.wait_for_timeout(4000)
    if not _web_client_started(page):
        logger.warning(
            "join click did not start the web client; dispatching a DOM click on the same control"
        )
        try:
            link.evaluate("el => el.click()")
        except Exception:
            logger.warning("DOM click raced with navigation; continuing")
    return None


def _web_client_started(page: Page) -> bool:
    if "/wc/" in page.url:
        return True
    return selectors_zoom.client_frame_present(page, timeout_ms=0)


def _wait_for_prejoin(page: Page, timeout_s: float = PREJOIN_READY_TIMEOUT_S) -> bool:
    """Wait until the client frame's pre-join controls have rendered."""
    deadline = time.monotonic() + timeout_s
    while True:
        if selectors_zoom.name_input(page, timeout_ms=0) is not None:
            return True
        if selectors_zoom.join_button(page, timeout_ms=0) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(1000)


def _set_display_name(page: Page, name_field: Locator, bot_name: str) -> None:
    """Type the consent name, then commit it through the form model.

    Live evidence (2026-10-05): Zoom's Vue form ignores Playwright keyboard
    events — the Join button stays disabled after ``type_text`` — while
    ``fill()`` lands the same value through the model path and enables the
    button. The humanized typing still runs so the visible behavior matches
    Meet's green room; the fill is the commit, not a second edit.
    """
    type_text(page, name_field, bot_name)
    name_field.fill(bot_name)
    logger.info("display name set: %s", bot_name)


def _run_prejoin(page: Page, bot_name: str) -> BotOutcome | None:
    """Name the bot and mute mic (required) / camera (warn) before joining."""
    dwell_before_start(page)
    name_field = selectors_zoom.name_input(page, timeout_ms=PREJOIN_READY_TIMEOUT_MS)
    if name_field is not None:
        _set_display_name(page, name_field, bot_name)
    else:
        debug_screenshot(page, "zoom name field not found")
        logger.warning("name field not found; joining with Zoom's default display name")
    pause_between_actions(page)
    if not _mute_media(
        page,
        selectors_zoom.microphone_toggle,
        "microphone",
        required=True,
        already_off_markers=_MIC_ALREADY_OFF,
    ):
        debug_screenshot(page, "microphone toggle not found")
        return BotOutcome(EXIT_BOT_ERROR, None, "microphone toggle not found")
    pause_between_actions(page)
    _mute_media(
        page,
        selectors_zoom.camera_toggle,
        "camera",
        required=False,
        already_off_markers=_CAMERA_ALREADY_OFF,
    )
    return None


def _click_join(page: Page) -> BotOutcome | None:
    """Click the pre-join Join control; the web client joins direct.

    Live evidence (2026-10-05): the Join button shares the landing control's
    trusted-click behavior — the UI can ignore a Playwright click — so a DOM
    click on the same control is dispatched when no transition is visible.
    """
    join = selectors_zoom.join_button(page, timeout_ms=JOIN_BUTTON_TIMEOUT_MS)
    if join is None:
        debug_screenshot(page, "zoom join button never appeared")
        logger.error("could not find a way to join the meeting")
        return BotOutcome(EXIT_BOT_ERROR, None, "join control never appeared")
    try:
        pause_between_actions(page)
        move_to(page, join)
        click_like_human(join)
    except Exception:
        debug_screenshot(page, "join control click failed")
        logger.error("join control click failed (disabled or covered)")
        return BotOutcome(EXIT_BOT_ERROR, None, "join control click failed")
    page.wait_for_timeout(4000)
    if _still_prejoin(page):
        logger.warning("join click did not transition; dispatching a DOM click on the same control")
        try:
            join.evaluate("el => el.click()")
        except Exception:
            logger.warning("DOM click raced with the join transition; continuing")
        page.wait_for_timeout(4000)
    # Zoom's web client submits the join directly (the waiting room, if any,
    # is detected after the click); Meet's knocking/direct distinction has no
    # pre-click Zoom equivalent.
    logger.info("join clicked (direct)")
    return None


def _still_prejoin(page: Page) -> bool:
    """Whether the pre-join form is still the only visible state."""
    if selectors_zoom.leave_button(page, timeout_ms=0) is not None:
        return False
    return selectors_zoom.join_button(page, timeout_ms=0) is not None


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


def run_zoom_spike_loop(
    page: Page,
    *,
    call_id: str,
    recorder: Recorder,
    timeouts: Timeouts,
    stop_requested: Callable[[], bool],
    poll_interval_s: float = POLL_INTERVAL_S,
) -> BotOutcome:
    """Z1 spike lifecycle loop — superseded by Z2's ``listeners_zoom.py``.

    Minimal supported states: join_clicked → waiting_room (or prejoin) →
    in_call → {call_ended (0), removed (3), give_up (0)}, plus waiting-room
    timeout (2) and the shared error paths (5). Alone/empty-room grace are
    deliberately absent; Z2 adds them with the full listening module.
    """
    start = _now()
    phase = _WAITING_ROOM
    _log_transition(call_id, _FROM_JOIN_CLICKED, phase, "join request submitted")

    admitted_at = 0.0
    recording_started = False
    next_admission_evidence = start + FIRST_ADMISSION_EVIDENCE_S

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
                    return BotOutcome(EXIT_BOT_ERROR, None, "recorder failed to start")
                recording_started = True
                admitted_at = now
                logger.info("recording started")
                _join_computer_audio(page)
                continue

            wall = _detect_fatal_wall(page)
            if wall is not None:
                _log_transition(call_id, phase, _BOT_ERROR, wall.reason)
                return wall

            if now - start >= timeouts.waiting_room_s:
                debug_screenshot(page, "never admitted")
                reason = f"waiting-room timeout after {timeouts.waiting_room_s} seconds"
                _log_transition(call_id, phase, "never_admitted", reason)
                logger.error("not admitted within %s seconds", timeouts.waiting_room_s)
                return BotOutcome(EXIT_NEVER_ADMITTED, None, reason)

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

        if removed:
            try:
                recorder.stop()
                logger.info("recording stopped")
            finally:
                _log_transition(call_id, phase, "removed", removed_detail)
            logger.info("removed from meeting")
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

        page.wait_for_timeout(int(poll_interval_s * 1000))


def _join_and_record_zoom(
    page: Page, meeting_url: str, bot_name: str, call_id: str, stop: _Stop
) -> tuple[BotOutcome, Recorder]:
    logger.info("navigating to meeting")
    recorder = Recorder(_wav_path(call_id))
    page.goto(meeting_url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")

    wall = _detect_fatal_wall(page)
    if wall is not None:
        return wall, recorder

    wall = _open_web_client(page)
    if wall is not None:
        return wall, recorder

    wall = _detect_fatal_wall(page)
    if wall is not None:
        return wall, recorder

    if not _wait_for_prejoin(page):
        debug_screenshot(page, "web-client pre-join never rendered")
        reason = "web-client pre-join never rendered"
        logger.error("%s", reason)
        return BotOutcome(EXIT_BOT_ERROR, None, reason), recorder

    outcome = _run_prejoin(page, bot_name)
    if outcome is not None:
        return outcome, recorder

    outcome = _click_join(page)
    if outcome is not None:
        return outcome, recorder

    try:
        loop_outcome = run_zoom_spike_loop(
            page,
            call_id=call_id,
            recorder=recorder,
            timeouts=_timeouts_from_env(),
            stop_requested=lambda: stop.requested,
        )
        return loop_outcome, recorder
    except Exception:
        if recorder.is_running():
            try:
                recorder.stop()
                logger.info("recording stopped")
            except Exception:
                logger.exception("failed to stop recorder during error handling")
        raise


def _run(meeting_url: str, bot_name: str, call_id: str, stop: _Stop) -> BotOutcome:
    with sync_playwright() as playwright:
        page, close = _launch_anonymous(playwright)
        try:
            outcome, _ = _join_and_record_zoom(page, meeting_url, bot_name, call_id, stop)
            return outcome
        finally:
            close()


def main() -> int:
    _configure_logging()
    meeting_url = os.environ.get("MEETING_URL", "").strip()
    bot_name = consent.resolve_bot_name()
    consent_ack = consent.consent_granted()
    call_id = os.environ.get("CALL_ID", "").strip() or "spike"

    logger.info(
        "oreeai bot starting: platform=zoom name=%s call_id=%s consent_ack=%s image_sha=%s",
        bot_name,
        call_id,
        consent_ack,
        os.environ.get("GIT_SHA", "unknown"),
    )
    if not consent_ack:
        logger.error(
            "CONSENT_ACK must be set to true to start the bot; refusing to join without "
            "explicit recording consent (exit %s). Set CONSENT_ACK=true once every "
            "participant has been told the call is recorded",
            EXIT_CONSENT_MISSING,
        )
        _emit_result(call_id, None, EXIT_CONSENT_MISSING)
        return EXIT_CONSENT_MISSING
    if not meeting_url:
        logger.error("MEETING_URL is required")
        _emit_result(call_id, None, EXIT_BOT_ERROR)
        return EXIT_BOT_ERROR

    stop = _Stop()

    def _on_signal(signum: int, frame: object) -> None:
        del frame
        logger.info("signal %s received; wrapping up", signum)
        stop.request()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    silence_floor = _silence_floor_from_env()
    outcome: BotOutcome | None = None
    exit_code = EXIT_BOT_ERROR
    try:
        outcome = _run(meeting_url, bot_name, call_id, stop)
        exit_code = _finalize_recording(outcome, _wav_path(call_id), silence_floor)
    except Exception:
        logger.exception("unexpected bot error")
        exit_code = EXIT_BOT_ERROR
    _emit_result(call_id, outcome, exit_code)
    logger.info(
        "bot finished: exit_code=%s end_reason=%s reason=%s",
        exit_code,
        outcome.end_reason if outcome is not None else None,
        outcome.reason if outcome is not None else "startup failed",
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
