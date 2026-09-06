"""Join a Google Meet call as a guest and record its audio to a WAV file.

PR 1 spike entry point — happy path plus a clean, graceful stop. The full
state machine (waiting room, removals, empty room, hard caps) lands in
PR 2; `bot/states.py` will absorb the state detection this file
currently inlines. Exit codes already follow the Shared contracts table:
0 clean end, 2 never admitted, 5 unexpected bot error.

Launches branded Google Chrome (channel="chrome"): Meet's server-side
anti-bot check detects Playwright's bundled Chromium build at join time.

    Env vars:
    MEETING_URL   (required) the https://meet.google.com/... link to join
    BOT_NAME      display name; spike-only - PR 3 hard-codes "Oree Notetaker"
    CONSENT_ACK   logged pass-through only; enforcement lands in PR 3
    CALL_ID       WAV file name, default "spike" (runner passes the real id)
    LOG_LEVEL     stdlib level name, default INFO
    DEBUG_DIR     where to write /tmp/oreeai-debug-<ts>.png on state stall;
                  default /tmp (which the spike sets to /debug via Makefile
                  so the screenshot survives the --rm container)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from bot import selectors
from bot.humanize import (
    click_like_human,
    dwell_before_start,
    move_to,
    pause_between_actions,
    type_text,
)
from bot.record_audio import Recorder
from bot.stealth import apply_stealth

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
DEBUG_SCREENSHOT_NAME = "oreeai-debug"
_TEXT_SNIPPET_CHARS = 300
JOIN_BLOCK_CHECK_TIMEOUT_MS = 4000
FIRST_ADMISSION_EVIDENCE_S = 5.0
ADMISSION_EVIDENCE_INTERVAL_S = 30.0

# Branded Google Chrome: Meet's server-side anti-bot check detects Playwright's
# bundled Chromium build at join time. The shared `launch_browser()` helper
# below is used by the bot and `bot/probe_browser.py` alike, so the probe
# cannot drift from the shipped launch config.
BROWSER_CHANNEL: str | None = "chrome"

_CHROMIUM_ARGS: tuple[str, ...] = (
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",
    "--lang=en-US",
    "--accept-lang=en-US,en",
    "--force-device-scale-factor=1.25",
    "--use-fake-ui-for-media-stream",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
)

# Playwright default launch flags that scream automation (dropped via
# ignore_default_args so the browser runs closer to a human install).
# --disable-features/--enable-features are deliberately NOT here: Playwright
# matches ignored args by exact full string, and those defaults carry values,
# so such entries never match — the residue is accepted (low-signal). Kept
# for function: --no-first-run, --password-store/--use-mock-keychain (no
# keyring in the container), --disable-search-engine-choice-screen (avoids a
# first-run modal), plus everything in _CHROMIUM_ARGS. Verified by the probe.
_BROWSER_IGNORED_ARGS: tuple[str, ...] = (
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-back-forward-cache",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-updater-scheduler",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--allow-pre-commit-input",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--disable-infobars",
    "--unsafely-disable-devtools-self-xss-warnings",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--edge-skip-compat-layer-relaunch",
    "--disable-edgeupdater",
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
        move_to(page, locator)
        click_like_human(locator)
        logger.info("%s muted before joining", label)
    else:
        logger.info("%s already off", label)


def _join_and_record(page: Page, meeting_url: str, bot_name: str, call_id: str, stop: _Stop) -> int:
    logger.info("navigating to meeting")
    page.goto(meeting_url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")

    if selectors.join_blocked_indicator(page, timeout_ms=JOIN_BLOCK_CHECK_TIMEOUT_MS) is not None:
        _debug_screenshot(page, "meeting blocks anonymous guests (host quick access off)")
        logger.error(
            "meeting blocks anonymous guests - host must enable Quick access in host controls"
        )
        return EXIT_BOT_ERROR

    dwell_before_start(page)
    name_field = selectors.name_input(page, timeout_ms=MEDIA_MUTE_TIMEOUT_MS)
    if name_field is not None:
        type_text(page, name_field, bot_name)
        logger.info("display name set: %s", bot_name)
    else:
        logger.warning("name field not found; joining with Meet's default display name")

    pause_between_actions(page)
    _mute_media(page, selectors.microphone_toggle, "microphone")
    pause_between_actions(page)
    _mute_media(page, selectors.camera_toggle, "camera")

    join = selectors.join_button(page, timeout_ms=int(JOIN_BUTTON_TIMEOUT_S * 1000))
    if join is None:
        _debug_screenshot(page, "join button never appeared")
        logger.error("could not find a way to join the meeting")
        return EXIT_BOT_ERROR
    knocking = (join.get_attribute("aria-label") or "").lower().startswith("ask")
    pause_between_actions(page)
    move_to(page, join)
    click_like_human(join)
    logger.info("join clicked (%s)", "knocking" if knocking else "direct")
    if knocking:
        logger.info("waiting to be admitted")

    deadline = time.monotonic() + ADMIT_TIMEOUT_S
    next_evidence = time.monotonic() + FIRST_ADMISSION_EVIDENCE_S
    admitted = False
    while not stop.requested:
        now = time.monotonic()
        if selectors.leave_call_button(page, timeout_ms=0) is not None:
            logger.info("admitted to the call")
            admitted = True
            break
        if selectors.join_blocked_indicator(page, timeout_ms=0) is not None:
            _debug_screenshot(page, "meet blocked the join attempt")
            logger.error("meet blocked the join attempt (anti-bot wall)")
            return EXIT_BOT_ERROR
        if now >= next_evidence:
            _debug_screenshot(page, "waiting for admission (periodic evidence)")
            next_evidence = now + ADMISSION_EVIDENCE_INTERVAL_S
        if selectors.knocking_indicator(page, timeout_ms=0) is not None:
            logger.info("still knocking (host has not admitted the bot yet)")
        if now >= deadline:
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


def launch_browser(playwright: Playwright) -> Browser:
    """Launch branded Chrome exactly as the bot ships it.

    Shared by `_run` and `bot/probe_browser.py` so the probe can never
    drift from the shipped launch config (channel, flags, ignored defaults).
    """
    return playwright.chromium.launch(
        channel=BROWSER_CHANNEL,
        headless=False,
        args=list(_CHROMIUM_ARGS),
        ignore_default_args=list(_BROWSER_IGNORED_ARGS),
    )


def _run(meeting_url: str, bot_name: str, call_id: str, stop: _Stop) -> int:
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context: BrowserContext = browser.new_context(
            locale="en-US",
            permissions=["microphone", "camera"],
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1.25,
        )
        apply_stealth(context)
        try:
            page: Page = context.new_page()
            return _join_and_record(page, meeting_url, bot_name, call_id, stop)
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
        "oreeai bot spike starting: name=%s call_id=%s consent_ack=%s image_sha=%s",
        bot_name,
        call_id,
        consent_ack,
        os.environ.get("GIT_SHA", "unknown"),
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
        return _run(meeting_url, bot_name, call_id, stop)
    except Exception:
        logger.exception("unexpected bot error")
        return EXIT_BOT_ERROR


if __name__ == "__main__":
    sys.exit(main())
