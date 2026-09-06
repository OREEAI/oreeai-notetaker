"""Human-like input timing for the green-room flow (PR 1 spike, option B).

Meet's join-time scoring sees an automated client; one cheap, legitimate
improvement is to stop acting like a script: dwell before touching
anything, move the mouse along a path to each target, type the name
key-by-key, hold clicks briefly, and pause between actions. All helpers
take a Playwright Page/Locator and use `page.wait_for_timeout` so signal
handling stays live. Selectors still live in `bot/selectors.py`; this
module owns only behavior.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


def _jitter(low: float, high: float) -> float:
    return random.uniform(low, high)


def dwell_before_start(page: Page) -> None:
    """Wait 4-8 s after load before the first interaction."""
    page.wait_for_timeout(int(_jitter(4000, 8000)))


def pause_between_actions(page: Page) -> None:
    """Wait 0.8-2.0 s between actions."""
    page.wait_for_timeout(int(_jitter(800, 2000)))


def move_to(page: Page, target: Locator) -> None:
    """Move the mouse to the target's center along a stepped path."""
    box = target.bounding_box()
    if box is None:
        return
    x = box["x"] + box["width"] / 2 + _jitter(-3.0, 3.0)
    y = box["y"] + box["height"] / 2 + _jitter(-3.0, 3.0)
    page.mouse.move(x, y, steps=int(_jitter(15, 25)))


def type_text(page: Page, field: Locator, text: str) -> None:
    """Click the field, then type it key-by-key with human jitter."""
    field.click()
    for char in text:
        page.wait_for_timeout(int(_jitter(40, 120)))
        page.keyboard.type(char)


def click_like_human(target: Locator) -> None:
    """Click with a human-length press and release."""
    target.click(delay=int(_jitter(60, 140)))
