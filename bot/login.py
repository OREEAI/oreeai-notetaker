"""One-time interactive Google sign-in for the bot's persistent profile.

Launches branded Chrome on the profile directory (same channel, flags, and
ignored defaults as :mod:`bot.join_meet` so the signed-in session belongs to
the exact browser the bot ships), opens the Google sign-in page, and waits
for a human to complete the sign-in through noVNC. Never navigates after the
initial page load: the human drives the page.

Run inside the container (``DISPLAY`` must point at a running X server):

    make bot-login

then open the printed noVNC address and sign in with the dedicated bot
account. Prints nothing sensitive: only state transitions and timeouts.
Exits 0 once a session is detected, 1 on timeout or unexpected error.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from playwright.sync_api import Page, sync_playwright

from bot.join_meet import (
    _BROWSER_IGNORED_ARGS,
    _CHROMIUM_ARGS,
    BROWSER_CHANNEL,
    validate_profile_dir,
)
from bot.states import is_signed_in

logger = logging.getLogger("oreeai.bot.login")

_SIGNIN_URL = "https://accounts.google.com"
_POLL_INTERVAL_S = 2.0
_LOGIN_TIMEOUT_S = 600.0
_GOTO_TIMEOUT_MS = 30000


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


def _login_timeout_s() -> float:
    raw = os.environ.get("BOT_LOGIN_TIMEOUT", "").strip()
    if not raw:
        return _LOGIN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid BOT_LOGIN_TIMEOUT %r; using default %s", raw, _LOGIN_TIMEOUT_S)
        return _LOGIN_TIMEOUT_S
    if value <= 0:
        logger.warning("out-of-range BOT_LOGIN_TIMEOUT %r; using default %s", raw, _LOGIN_TIMEOUT_S)
        return _LOGIN_TIMEOUT_S
    return value


def _wait_for_sign_in(page: Page, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        signed_in, detail = is_signed_in(page)
        if signed_in:
            logger.info("google session established (%s)", detail)
            return True
        page.wait_for_timeout(int(_POLL_INTERVAL_S * 1000))
    return False


def main() -> int:
    _configure_logging()
    logger.info("image_sha=%s", os.environ.get("GIT_SHA", "unknown"))
    if "DISPLAY" not in os.environ:
        logger.error("DISPLAY is not set; login must run headful under Xvfb (make bot-login)")
        return 1

    profile_dir = os.environ.get("BOT_PROFILE_DIR", "").strip() or "/profile"
    problem = validate_profile_dir(profile_dir)
    if problem is not None:
        logger.error("cannot use profile directory /profile: %s", problem)
        return 1

    timeout_s = _login_timeout_s()
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile_dir,
                channel=BROWSER_CHANNEL,
                headless=False,
                args=list(_CHROMIUM_ARGS),
                ignore_default_args=list(_BROWSER_IGNORED_ARGS),
                locale="en-US",
            )
            try:
                existing = context.pages
                page = existing[0] if existing else context.new_page()
                page.goto(_SIGNIN_URL, timeout=_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
                logger.info(
                    "sign in with the dedicated bot account in the noVNC window, "
                    "then wait; timeout %s seconds",
                    timeout_s,
                )
                if _wait_for_sign_in(page, timeout_s):
                    logger.info("login complete; future authenticated runs will reuse this session")
                    return 0
            finally:
                context.close()
    except Exception:
        logger.exception("login bootstrap failed")
        return 1
    logger.error("sign-in not completed within %s seconds", timeout_s)
    return 1


if __name__ == "__main__":
    sys.exit(main())
