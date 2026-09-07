"""Join a Google Meet call as a guest and record its audio to a WAV file.

PR 2 lifecycle entry point: waiting room, late admission, never admitted,
removals, empty rooms, alone grace, maximum recording duration, and clean
shutdown. DOM predicates live in :mod:`bot.states`; polling, timing, state
transitions, and recorder triggers live in :mod:`bot.listeners`. Exit codes
follow the Shared contracts table, including 3 for removal, 4 for lifecycle
timeouts, and 7 when a clean recording is silent.

Launches branded Google Chrome (channel="chrome"): Meet's server-side
anti-bot check detects Playwright's bundled Chromium build at join time.

    Env vars:
    MEETING_URL   (required) the https://meet.google.com/... link to join
    BOT_NAME      display name; spike-only - PR 3 hard-codes "Oree Notetaker"
    CONSENT_ACK   logged pass-through only; enforcement lands in PR 3
    CALL_ID       WAV file name, default "spike" (runner passes the real id)
    LOG_LEVEL     stdlib level name, default INFO
    DEBUG_DIR     where to write /tmp/oreeai-debug-<ts>.png on state stall;
                  default /tmp (which local runs set to /debug via Makefile
                  so the screenshot survives the --rm container)
    BOT_WAITING_ROOM_TIMEOUT
                  seconds waiting for admission before exit 2, default 600
    BOT_EMPTY_ROOM_TIMEOUT
                  seconds in an empty or undetectable room before exit 4/5,
                  default 300
    BOT_ALONE_GRACE
                  seconds to remain after other participants leave before a
                  clean exit 0, default 60
    BOT_MAX_RECORD_DURATION
                  maximum recording seconds before a clean exit 0, default 10800
    BOT_SILENCE_RMS_FLOOR
                  whole-WAV RMS floor, in raw 16-bit units, default 50
    PAREC_DEVICE  diagnostic PulseAudio-device override for parec; default is
                  the operational virtual-speaker monitor
    BOT_AUTH_MODE join identity: "anonymous" (guest, default) or
                  "authenticated" (signed-in Google account via a persistent
                  Chrome profile). Unknown values warn and fall back to
                  anonymous.
    BOT_PROFILE_DIR
                  container path of the persistent Chrome profile used in
                  authenticated mode, default /profile (mounted from the
                  host's bot/chrome-profile by the Makefile)
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from bot import selectors, states
from bot.humanize import (
    click_like_human,
    dwell_before_start,
    move_to,
    pause_between_actions,
    type_text,
)
from bot.listeners import (
    EXIT_BOT_ERROR,
    EXIT_OK,
    EXIT_SILENT_RECORDING,
    BotOutcome,
    Timeouts,
    debug_screenshot,
    run_call_loop,
)
from bot.record_audio import Recorder, check_recording
from bot.stealth import apply_stealth

if TYPE_CHECKING:
    from playwright.sync_api import Locator

logger = logging.getLogger("oreeai.bot")
result_logger = logging.getLogger("oreeai.bot.result")

JOIN_BUTTON_TIMEOUT_S = 30.0
MEDIA_MUTE_TIMEOUT_MS = 5000
GOTO_TIMEOUT_MS = 60000
JOIN_BLOCK_CHECK_TIMEOUT_MS = 4000

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
    raw = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw, None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not isinstance(getattr(logging, raw, None), int):
        logger.warning("unknown LOG_LEVEL %r; using INFO", raw)


class _Stop:
    def __init__(self) -> None:
        self._requested = False

    def request(self) -> None:
        self._requested = True

    @property
    def requested(self) -> bool:
        return self._requested


def _mute_media(
    page: Page,
    toggle: Callable[..., Locator | None],
    label: str,
    *,
    required: bool = False,
) -> bool:
    """Mute a pre-join toggle. Returns True when muted or already off.

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
    aria = locator.get_attribute("aria-label") or ""
    if aria.lower().startswith("turn off"):
        move_to(page, locator)
        click_like_human(locator)
        logger.info("%s muted before joining", label)
    else:
        logger.info("%s already off", label)
    return True


def _env_float(name: str, default: float, *, maximum: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid %s %r; using default %s", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0 or (maximum is not None and value > maximum):
        logger.warning("out-of-range %s %r; using default %s", name, raw, default)
        return default
    return value


def _timeouts_from_env() -> Timeouts:
    return Timeouts(
        waiting_room_s=_env_float("BOT_WAITING_ROOM_TIMEOUT", 600.0),
        empty_room_s=_env_float("BOT_EMPTY_ROOM_TIMEOUT", 300.0),
        alone_grace_s=_env_float("BOT_ALONE_GRACE", 60.0),
        max_record_s=_env_float("BOT_MAX_RECORD_DURATION", 10800.0),
    )


def _silence_floor_from_env() -> float:
    return _env_float("BOT_SILENCE_RMS_FLOOR", 50.0, maximum=32767.0)


AUTH_MODE_ANONYMOUS = "anonymous"
AUTH_MODE_AUTHENTICATED = "authenticated"
PROFILE_DIR_DEFAULT = "/profile"
SESSION_CHECK_URL = "https://myaccount.google.com"
SESSION_CHECK_TIMEOUT_MS = 15000


def parse_auth_mode(raw: str) -> str:
    """Normalize BOT_AUTH_MODE; unknown values fall back to anonymous."""
    mode = raw.strip().lower()
    if mode in ("", AUTH_MODE_ANONYMOUS):
        return AUTH_MODE_ANONYMOUS
    if mode == AUTH_MODE_AUTHENTICATED:
        return AUTH_MODE_AUTHENTICATED
    logger.warning("unknown BOT_AUTH_MODE %r; using %s", raw, AUTH_MODE_ANONYMOUS)
    return AUTH_MODE_ANONYMOUS


def validate_profile_dir(path: str) -> str | None:
    """Return an error string when the profile dir is unusable, else None."""
    if not path:
        return "profile directory is not configured"
    if not os.path.isdir(path):
        return "profile directory is missing (run make bot-login first)"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return "profile directory is not readable and writable"
    return None


def _wav_path(call_id: str) -> str:
    return f"/audio/{call_id}.wav"


def _check_session(page: Page) -> BotOutcome | None:
    """Verify the persistent profile holds a live Google session.

    Returns an error outcome when the session is missing or invalid, else
    None. A bad session is a pre-join failure (exit 5); callers keep the
    detail in the failure reason for the future runner.
    """
    try:
        page.goto(
            SESSION_CHECK_URL, timeout=SESSION_CHECK_TIMEOUT_MS, wait_until="domcontentloaded"
        )
    except Exception:
        reason = "google session check page failed to load"
        logger.error("%s", reason)
        return BotOutcome(EXIT_BOT_ERROR, None, reason)
    signed_in, detail = states.is_signed_in(page)
    if signed_in:
        logger.info("google session active (%s)", detail)
        return None
    reason = f"google session invalid: {detail} (run make bot-login to sign in)"
    logger.error("%s", reason)
    return BotOutcome(EXIT_BOT_ERROR, None, reason)


def _join_and_record(
    page: Page, meeting_url: str, bot_name: str, call_id: str, stop: _Stop, authenticated: bool
) -> tuple[BotOutcome, Recorder]:
    logger.info("navigating to meeting")
    recorder = Recorder(_wav_path(call_id))
    page.goto(meeting_url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")

    if selectors.join_blocked_indicator(page, timeout_ms=JOIN_BLOCK_CHECK_TIMEOUT_MS) is not None:
        debug_screenshot(page, "meeting blocks anonymous guests (host quick access off)")
        logger.error(
            "meeting blocks anonymous guests - host must enable Quick access in host controls"
        )
        return BotOutcome(EXIT_BOT_ERROR, None, "meeting blocks anonymous guests"), recorder

    dwell_before_start(page)
    if authenticated:
        logger.info("authenticated mode: joining with the account display name")
    else:
        name_field = selectors.name_input(page, timeout_ms=MEDIA_MUTE_TIMEOUT_MS)
        if name_field is not None:
            type_text(page, name_field, bot_name)
            logger.info("display name set: %s", bot_name)
        else:
            logger.warning("name field not found; joining with Meet's default display name")
    pause_between_actions(page)
    if not _mute_media(page, selectors.microphone_toggle, "microphone", required=True):
        debug_screenshot(page, "microphone toggle not found")
        return BotOutcome(EXIT_BOT_ERROR, None, "microphone toggle not found"), recorder
    pause_between_actions(page)
    _mute_media(page, selectors.camera_toggle, "camera")

    join = selectors.join_button(page, timeout_ms=int(JOIN_BUTTON_TIMEOUT_S * 1000))
    if join is None:
        debug_screenshot(page, "join button never appeared")
        logger.error("could not find a way to join the meeting")
        return BotOutcome(EXIT_BOT_ERROR, None, "join control never appeared"), recorder
    knocking = (join.get_attribute("aria-label") or "").lower().startswith("ask")
    pause_between_actions(page)
    move_to(page, join)
    click_like_human(join)
    logger.info("join clicked (%s)", "knocking" if knocking else "direct")
    if knocking:
        logger.info("waiting to be admitted")

    try:
        outcome = run_call_loop(
            page,
            call_id=call_id,
            recorder=recorder,
            timeouts=_timeouts_from_env(),
            stop_requested=lambda: stop.requested,
        )
        return outcome, recorder
    except Exception:
        if recorder.is_running():
            try:
                recorder.stop()
                logger.info("recording stopped")
            except Exception:
                logger.exception("failed to stop recorder during error handling")
        raise


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


def _launch_anonymous(playwright: Playwright) -> tuple[Page, Callable[[], None]]:
    """Launch branded Chrome with a throwaway profile (guest joins)."""
    browser = launch_browser(playwright)
    context: BrowserContext = browser.new_context(
        locale="en-US",
        permissions=["microphone", "camera"],
        viewport={"width": 1920, "height": 1080},
        device_scale_factor=1.25,
    )
    apply_stealth(context)

    def _close() -> None:
        context.close()
        browser.close()

    return context.new_page(), _close


def _launch_authenticated(
    playwright: Playwright, profile_dir: str
) -> tuple[Page, Callable[[], None]]:
    """Launch branded Chrome on the persistent signed-in profile.

    Uses the same channel, flags, ignored defaults, and context options as
    the anonymous path so the probe's fingerprint stays representative.
    """
    context: BrowserContext = playwright.chromium.launch_persistent_context(
        profile_dir,
        channel=BROWSER_CHANNEL,
        headless=False,
        args=list(_CHROMIUM_ARGS),
        ignore_default_args=list(_BROWSER_IGNORED_ARGS),
        locale="en-US",
        permissions=["microphone", "camera"],
        viewport={"width": 1920, "height": 1080},
        device_scale_factor=1.25,
    )
    apply_stealth(context)

    def _close() -> None:
        context.close()

    existing = context.pages
    return (existing[0] if existing else context.new_page()), _close


def _run(
    meeting_url: str, bot_name: str, call_id: str, stop: _Stop, auth_mode: str, profile_dir: str
) -> BotOutcome:
    with sync_playwright() as playwright:
        if auth_mode == AUTH_MODE_AUTHENTICATED:
            problem = validate_profile_dir(profile_dir)
            if problem is not None:
                return BotOutcome(EXIT_BOT_ERROR, None, f"authenticated mode: {problem}")
            page, close = _launch_authenticated(playwright, profile_dir)
        else:
            page, close = _launch_anonymous(playwright)
        try:
            if auth_mode == AUTH_MODE_AUTHENTICATED:
                session_error = _check_session(page)
                if session_error is not None:
                    return session_error
            outcome, _ = _join_and_record(
                page, meeting_url, bot_name, call_id, stop, auth_mode == AUTH_MODE_AUTHENTICATED
            )
            return outcome
        finally:
            close()


def _finalize_recording(outcome: BotOutcome, wav_path: str, silence_floor: float) -> int:
    """Apply the post-recording silence check to an otherwise clean exit."""
    if outcome.exit_code != EXIT_OK or not outcome.recording_started:
        return outcome.exit_code

    check = check_recording(wav_path, silence_floor)
    if check.status == "ok":
        return EXIT_OK
    if check.status == "silent":
        logger.error(
            "recording below silence floor: rms=%.1f floor=%.1f",
            check.rms or 0.0,
            silence_floor,
        )
        return EXIT_SILENT_RECORDING
    logger.error("cannot verify finished recording: %s", check.detail)
    return EXIT_BOT_ERROR


def _emit_result(call_id: str, outcome: BotOutcome | None, exit_code: int) -> None:
    """Emit the machine-readable terminal line consumed by the future runner."""
    payload = {
        "call_id": call_id,
        "end_reason": outcome.end_reason if outcome is not None else None,
        "exit_code": exit_code,
    }
    result_logger.info("OREEAI_BOT_RESULT %s", json.dumps(payload, sort_keys=True))


def main() -> int:
    _configure_logging()
    meeting_url = os.environ.get("MEETING_URL", "").strip()
    bot_name = os.environ.get("BOT_NAME", "Oree Spike")
    consent_ack = os.environ.get("CONSENT_ACK", "").strip().lower() in ("1", "true", "yes")
    call_id = os.environ.get("CALL_ID", "").strip() or "spike"
    auth_mode = parse_auth_mode(os.environ.get("BOT_AUTH_MODE", ""))
    profile_dir = os.environ.get("BOT_PROFILE_DIR", "").strip() or PROFILE_DIR_DEFAULT

    if not meeting_url:
        logger.error("MEETING_URL is required")
        _emit_result(call_id, None, EXIT_BOT_ERROR)
        return EXIT_BOT_ERROR
    logger.info(
        "oreeai bot starting: name=%s call_id=%s consent_ack=%s auth_mode=%s image_sha=%s",
        bot_name,
        call_id,
        consent_ack,
        auth_mode,
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

    silence_floor = _silence_floor_from_env()
    outcome: BotOutcome | None = None
    exit_code = EXIT_BOT_ERROR
    try:
        outcome = _run(meeting_url, bot_name, call_id, stop, auth_mode, profile_dir)
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
