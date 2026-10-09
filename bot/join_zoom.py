"""Join a Zoom meeting through the web client as a guest and record its audio.

Full lifecycle parity with Meet: waiting room, host-not-started, never
admitted, removals, empty rooms, alone grace, maximum recording duration,
stop requests, and the shared silence check. DOM predicates live in
:mod:`bot.states_zoom`; polling, timing, transitions, and recorder triggers
live in :mod:`bot.listeners_zoom`. Exit codes follow the shared table in
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
                  seconds waiting for admission (waiting room or
                  host-not-started screen) before exit 2, default 600
    BOT_EMPTY_ROOM_TIMEOUT
                  seconds in an empty or undetectable room after admission
                  before exit 4/5, default 300
    BOT_ALONE_GRACE
                  seconds to remain after other participants leave before a
                  clean exit 0, default 60
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
from typing import TYPE_CHECKING

from playwright.sync_api import sync_playwright

from bot import consent, selectors_zoom
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
    _configure_logging,
    _emit_result,
    _finalize_recording,
    _launch_anonymous,
    _silence_floor_from_env,
    _Stop,
    _timeouts_from_env,
    _wav_path,
)
from bot.listeners import EXIT_BOT_ERROR, EXIT_CONSENT_MISSING, BotOutcome, debug_screenshot
from bot.listeners_zoom import (
    _CAMERA_ALREADY_OFF,
    _MIC_ALREADY_OFF,
    _detect_fatal_wall,
    _mute_media,
    run_call_loop,
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


def _announce_consent(page: Page) -> None:
    """Listener hook: post the consent message once recording has started."""
    consent.post_chat_announcement(page, selector_set=selectors_zoom)


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
        loop_outcome = run_call_loop(
            page,
            call_id=call_id,
            recorder=recorder,
            timeouts=_timeouts_from_env(),
            stop_requested=lambda: stop.requested,
            announce=_announce_consent,
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
