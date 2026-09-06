"""Init-script fingerprint patches for the Meet join (PR 1 spike, option B).

Covers the client-environment signals the B1 audit proved differ from a
real Chrome install and that launch flags cannot fix: the software WebGL
renderer string (the container keeps SwiftShader even with /dev/dri passed
through and ANGLE-on-GL requested). The spoofed strings are the exact
values from the same-host Chrome incognito dump in the Handoff Notes.

Applied identically to the bot and the probe via `apply_stealth`, so the
verified fingerprint is the shipped one. Deliberately narrow: a WeakSet
guard (no marker properties on page objects) plus toString masking on the
wrapped methods, so the patch itself adds no new enumerable artifacts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

_VENDOR = "Google Inc. (Intel)"
_RENDERER = "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL ES 3.2)"

_SPOOF_JS = (
    "(() => {\n"
    f"  const VENDOR = {json.dumps(_VENDOR)};\n"
    f"  const RENDERER = {json.dumps(_RENDERER)};\n"
    """  const patched = new WeakSet();
  const nativeToString = Function.prototype.toString;
  const mask = (fn, orig) => {
    try {
      Object.defineProperty(fn, "toString", { value: nativeToString.bind(orig) });
    } catch (e) { /* leave unmasked */ }
  };
  const wrapContext = (ctx) => {
    const proto = Object.getPrototypeOf(ctx);
    if (patched.has(proto)) return;
    patched.add(proto);
    const origGetParameter = proto.getParameter;
    const getParameter = function (p) {
      if (p === 0x9245) return VENDOR;
      if (p === 0x9246) return RENDERER;
      return origGetParameter.call(this, p);
    };
    mask(getParameter, origGetParameter);
    proto.getParameter = getParameter;
    const origGetExtension = proto.getExtension;
    const getExtension = function (name) {
      if (name === "WEBGL_debug_renderer_info") {
        return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
      }
      return origGetExtension.call(this, name);
    };
    mask(getExtension, origGetExtension);
    proto.getExtension = getExtension;
  };
  try {
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    const getContext = function (type, ...args) {
      const ctx = origGetContext.call(this, type, ...args);
      if (ctx && (type === "webgl" || type === "experimental-webgl"
          || type === "webgl2")) {
        try { wrapContext(ctx); } catch (e) { /* leave unpatched */ }
      }
      return ctx;
    };
    mask(getContext, origGetContext);
    HTMLCanvasElement.prototype.getContext = getContext;
  } catch (e) { /* leave unpatched */ }
})();"""
)


def apply_stealth(context: BrowserContext) -> None:
    """Install the fingerprint init script on every page of the context."""
    context.add_init_script(_SPOOF_JS)
