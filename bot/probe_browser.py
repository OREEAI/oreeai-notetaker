"""In-container browser launch probe (dev tool, not part of the bot flow).

Verifies that the shipped launch call — imported verbatim from
`bot.join_meet.launch_browser` so this probe cannot drift from what the bot
actually ships — launches headful under Xvfb and does not advertise
automation (`navigator.webdriver` reads false).

Also dumps a bounded client fingerprint (`fingerprint=<json>`) and the
browser's real launch argv read from /proc (`browser_argv=<...>`), so a
fingerprint diff against the same fields dumped from a real Chrome
incognito session can rank what still distinguishes the automated client.

The fingerprint contains no identifiers: device counts, not device IDs;
media-device label presence, not labels; plugin names, not paths.

Run inside the container (`DISPLAY` must point at a running X server):

    make bot-probe

Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from playwright.sync_api import Page, sync_playwright

from bot.join_meet import BROWSER_CHANNEL, launch_browser
from bot.stealth import apply_stealth

logger = logging.getLogger("oreeai.bot.probe")

_FINGERPRINT_JS = """() => {
  const out = {};
  const safe = (fn) => {
    try {
      const v = fn();
      return v === undefined ? "ABSENT" : v;
    } catch (e) { return "ERROR:" + (e && e.message ? e.message : String(e)); }
  };
  out.webdriver = safe(() => navigator.webdriver);
  out.userAgent = safe(() => navigator.userAgent);
  out.vendor = safe(() => navigator.vendor);
  out.platform = safe(() => navigator.platform);
  out.languages = safe(() => navigator.languages);
  out.hardwareConcurrency = safe(() => navigator.hardwareConcurrency);
  out.deviceMemory = safe(() => navigator.deviceMemory);
  out.cookieEnabled = safe(() => navigator.cookieEnabled);
  out.plugins = safe(() => Array.from(navigator.plugins).map((p) => p.name));
  out.chromeKeys = safe(() => (window.chrome ? Object.keys(window.chrome) : null));
  out.timeZone = safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  out.notificationPermission = safe(() => Notification.permission);
  out.uaBrands = safe(() => (navigator.userAgentData ? navigator.userAgentData.brands : null));
  out.uaDataPresent = safe(() => !!navigator.userAgentData);
  out.mediaDevicesPresent = safe(() => !!navigator.mediaDevices);
  out.webgl = safe(() => {
    const c = document.createElement("canvas");
    const ctx = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (!ctx) return null;
    const dbg = ctx.getExtension("WEBGL_debug_renderer_info");
    return {
      vendor: ctx.getParameter(ctx.VENDOR),
      renderer: ctx.getParameter(ctx.RENDERER),
      unmaskedVendor: dbg ? ctx.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
      unmaskedRenderer: dbg ? ctx.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    };
  });
  out.screen = safe(() => ({
    width: screen.width, height: screen.height,
    availWidth: screen.availWidth, availHeight: screen.availHeight,
    colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
  }));
  out.window = safe(() => ({
    innerWidth: window.innerWidth, innerHeight: window.innerHeight,
    outerWidth: window.outerWidth, outerHeight: window.outerHeight,
    devicePixelRatio: window.devicePixelRatio,
  }));
  out.fonts = safe(() => {
    const c = document.createElement("canvas").getContext("2d");
    if (!c) return null;
    const s = "mmmmmmmmmmlli";
    const names = ["Arial", "Helvetica", "Times New Roman", "Courier New",
      "Verdana", "Georgia", "Tahoma", "Trebuchet MS", "DejaVu Sans",
      "Liberation Sans", "Noto Sans", "monospace", "sans-serif", "serif"];
    const r = {};
    for (const n of names) {
      c.font = '72px "' + n + '"';
      r[n] = Math.round(c.measureText(s).width * 100) / 100;
    }
    return r;
  });
  return out;
}"""

_UA_HIGH_ENTROPY_JS = (
    """() => (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues"""
    """ ? navigator.userAgentData.getHighEntropyValues(["platform", "platformVersion","""
    """     "architecture", "model", "bitness", "fullVersionList", "uaFullVersion"])"""
    """     .catch(() => null) : Promise.resolve(null))"""
)

_PERMISSIONS_JS = """() => Promise.all(["camera", "microphone", "notifications"].map((n) =>
  navigator.permissions.query({ name: n }).then((p) => p.state).catch(() => null)))"""

_MEDIA_DEVICES_JS = """() => (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices
  ? navigator.mediaDevices.enumerateDevices().then((ds) => ({
      count: ds.length,
      kinds: ds.map((d) => d.kind),
      anyLabel: ds.some((d) => !!d.label),
    })).catch(() => null)
  : Promise.resolve(null))"""


def _collect_fingerprint(page: Page) -> dict[str, Any]:
    """Evaluate the bounded fingerprint on a real HTTPS page; mirrors the dump HTML."""
    fp: dict[str, Any] = page.evaluate(_FINGERPRINT_JS)
    fp["uaHighEntropy"] = page.evaluate(_UA_HIGH_ENTROPY_JS)
    fp["permissions"] = page.evaluate(_PERMISSIONS_JS)
    fp["mediaDevices"] = page.evaluate(_MEDIA_DEVICES_JS)
    return fp


def _browser_argv() -> str | None:
    """Return the Playwright-driven browser's real launch argv from /proc.

    /proc is only readable this way inside the container; None elsewhere.
    Matches the main browser process (the one holding --remote-debugging-pipe).
    """
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if not args:
            continue
        if os.path.basename(args[0]) == "chrome" and "--remote-debugging-pipe" in args:
            return " ".join(args)
    return None


def main() -> int:
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
    logger.info("image_sha=%s", os.environ.get("GIT_SHA", "unknown"))
    if "DISPLAY" not in os.environ:
        logger.error("DISPLAY is not set; probe must run headful under Xvfb (make bot-probe)")
        return 1
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright)
            try:
                context = browser.new_context(
                    locale="en-US",
                    permissions=["microphone", "camera"],
                    viewport={"width": 1920, "height": 1080},
                    device_scale_factor=1.25,
                )
                apply_stealth(context)
                try:
                    page = context.new_page()
                    page.goto(
                        "https://example.com",
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                    webdriver = page.evaluate("() => navigator.webdriver")
                    user_agent = page.evaluate("() => navigator.userAgent")
                    fingerprint = _collect_fingerprint(page)
                    browser_argv = _browser_argv()
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
    logger.info("fingerprint=%s", json.dumps(fingerprint, sort_keys=True, default=str))
    logger.info("browser_argv=%s", browser_argv or "<unavailable outside the container>")
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
