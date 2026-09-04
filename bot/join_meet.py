"""Join a Google Meet call as a guest and record its audio to a WAV file.

PR 1 spike entry point — happy path plus a clean, graceful stop. The full
state machine (waiting room, removals, empty room, hard caps) lands in
PR 2; `bot/states.py` will absorb the state detection this file
currently inlines. Exit codes already follow the Shared contracts table:
0 clean end, 2 never admitted, 5 unexpected bot error.

Env vars:
    MEETING_URL   (required) the https://meet.google.com/... link to join
    BOT_NAME      display name; spike-only — PR 3 hard-codes "Oree Notetaker"
    CONSENT_ACK   logged pass-through only; enforcement lands in PR 3
    CALL_ID       WAV file name, default "spike" (runner passes the real id)
    LOG_LEVEL     stdlib level name, default INFO
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Page, sync_playwright

from bot import selectors
from bot.record_audio import Recorder

if TYPE_CHECKING:
    from playwright.sync_api import Locator

logger = logging.getLogger("oreeai.bot")

EXIT_OK = 0
EXIT_NEVER_ADMITTED = 2
EXIT_BOT_ERROR = 5

POLL_INTERVAL_S = 2.0
JOIN_BUTTON_TIMEOUT_S = 30.0
ADMIT_TIMEOUT_S = 600.0
ENDED_CONFIRMATION_POLLS = 3
MEDIA_MUTE_TIMEOUT_MS = 5000
GOTO_TIMEOUT_MS = 60000
DEBUG_SCREENSHOT_PREFIX = "/tmp/oreeai-debug"

_CHROMIUM_ARGS: tuple[str, ...] = (
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",
    "--lang=en-US",
    "--use-fake-ui-for-media-stream",
    "--no-sandbox",
)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class _Stop:
    def __init__(self) -> None:
        self._requested = False

    def request(self) -> None:
        self._requested = True

    @property
    def requested(self) -> bool:
        return self._requested


def _debug_screenshot(page: Page, reason: str) -> None:
    path = f"{DEBUG_SCREENSHOT_PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        page.screenshot(path=path)
        logger.warning("state detection stalled (%s); saved debug screenshot to %s", reason, path)
    except Exception:
        logger.exception("failed to save debug screenshot (%s)", reason)


def _mute_media(
    page: Page,
    toggle: Callable[..., Locator | None],
    label: str,
) -> None:
    locator = toggle(page, timeout_ms=MEDIA_MUTE_TIMEOUT_MS)
    if locator is None:
        logger.warning("%s toggle not found; continuing without muting", label)
        return
    aria = locator.get_attribute("aria-label") or ""
    if aria.lower().startswith("turn off"):
        locator.click()
        logger.info("%s muted before joining", label)
    else:
        logger.info("%s already off", label)


def _join_and_record(page: Page, meeting_url: str, call_id: str, stop: _Stop) -> int:
    logger.info("navigating to meeting")
    page.goto(meeting_url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")

    _mute_media(page, selectors.microphone_toggle, "microphone")
    _mute_media(page, selectors.camera_toggle, "camera")

    join = selectors.join_button(page, timeout_ms=int(JOIN_BUTTON_TIMEOUT_S * 1000))
    if join is None:
        _debug_screenshot(page, "join button never appeared")
        logger.error("could not find a way to join the meeting")
        return EXIT_BOT_ERROR
    knocking = (join.get_attribute("aria-label") or "").lower().startswith("ask")
    join.click()
    logger.info("join clicked (%s)", "knocking" if knocking else "direct")
    if knocking:
        logger.info("waiting to be admitted")

    deadline = time.monotonic() + ADMIT_TIMEOUT_S
    admitted = False
    while not stop.requested:
        if selectors.leave_call_button(page, timeout_ms=0) is not None:
            logger.info("admitted to the call")
            admitted = True
            break
        if selectors.knocking_indicator(page, timeout_ms=0) is not None:
            logger.info("still knocking (host has not admitted the bot yet)")
        if time.monotonic() >= deadline:
            _debug_screenshot(page, "never admitted")
            logger.error("not admitted within %s seconds", ADMIT_TIMEOUT_S)
            return EXIT_NEVER_ADMITTED
        page.wait_for_timeout(int(POLL_INTERVAL_S * 1000))
    if not admitted:
        logger.info("stop requested before admission; leaving without recording")
        return EXIT_OK

    recorder = Recorder(f"/audio/{call_id}.wav")
    recorder.start()
    logger.info("recording started")
    missed = 0
    while not stop.requested:
        page.wait_for_timeout(int(POLL_INTERVAL_S * 1000))
        if selectors.leave_call_button(page, timeout_ms=0) is not None:
            missed = 0
            continue
        missed += 1
        if selectors.call_ended_indicator(page, timeout_ms=500) is not None:
            logger.info("call ended")
            break
        if missed >= ENDED_CONFIRMATION_POLLS:
            logger.info("in-call indicators gone for %s polls; treating as call ended", missed)
            break
    recorder.stop()
    logger.info("recording stopped")
    return EXIT_OK


def _run(meeting_url: str, call_id: str, stop: _Stop) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=list(_CHROMIUM_ARGS))
        context: BrowserContext = browser.new_context(
            locale="en-US",
            permissions=["audioCapture", "videoCapture"],
            viewport={"width": 1280, "height": 720},
        )
        try:
            page: Page = context.new_page()
            return _join_and_record(page, meeting_url, call_id, stop)
        finally:
            context.close()
            browser.close()


def main() -> int:
    _configure_logging()
    meeting_url = os.environ.get("MEETING_URL", "").strip()
    bot_name = os.environ.get("BOT_NAME", "Oree Spike")
    consent_ack = os.environ.get("CONSENT_ACK", "").strip().lower() in ("1", "true", "yes")
    call_id = os.environ.get("CALL_ID", "").strip() or "spike"

    if not meeting_url:
        logger.error("MEETING_URL is required")
        return EXIT_BOT_ERROR
    logger.info(
        "oreeai bot spike starting: name=%s call_id=%s consent_ack=%s",
        bot_name,
        call_id,
        consent_ack,
    )
    if not consent_ack:
        logger.warning("CONSENT_ACK not set; accepted in the spike, enforcement lands in PR 3")

    stop = _Stop()

    def _on_signal(signum: int, frame: object) -> None:
        del frame
        logger.info("signal %s received; wrapping up", signum)
        stop.request()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        return _run(meeting_url, call_id, stop)
    except Exception:
        logger.exception("unexpected bot error")
        return EXIT_BOT_ERROR


if __name__ == "__main__":
    sys.exit(main())
