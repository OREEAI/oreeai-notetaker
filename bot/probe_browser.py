"""In-container browser launch probe (dev tool, not part of the bot flow).

Verifies that the shipped launch config — the Playwright channel plus the
Chromium flags, both imported verbatim from `bot.join_meet` so this probe
cannot drift from what the bot actually ships — launches headful under Xvfb
and does not advertise automation (`navigator.webdriver` reads false).

Run inside the container (`DISPLAY` must point at a running X server):

    make bot-probe

Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import logging
import os
import sys

from playwright.sync_api import sync_playwright

from bot.join_meet import _CHROMIUM_ARGS, BROWSER_CHANNEL

logger = logging.getLogger("oreeai.bot.probe")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if "DISPLAY" not in os.environ:
        logger.error("DISPLAY is not set; probe must run headful under Xvfb (make bot-probe)")
        return 1
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=BROWSER_CHANNEL,
                headless=False,
                args=list(_CHROMIUM_ARGS),
            )
            try:
                context = browser.new_context(
                    locale="en-US",
                    permissions=["microphone", "camera"],
                    viewport={"width": 1280, "height": 720},
                )
                try:
                    page = context.new_page()
                    webdriver = page.evaluate("() => navigator.webdriver")
                    user_agent = page.evaluate("() => navigator.userAgent")
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception:
        logger.exception("probe failed: browser did not launch or evaluate")
        return 1
    logger.info("channel=%s", BROWSER_CHANNEL)
    logger.info("user_agent=%s", user_agent)
    logger.info("navigator.webdriver=%s", webdriver)
    if webdriver:
        logger.error("probe failed: navigator.webdriver is truthy")
        return 1
    if "Headless" in str(user_agent):
        logger.error("probe failed: user agent advertises headless")
        return 1
    logger.info("probe passed: branded channel launches headful, webdriver hidden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
