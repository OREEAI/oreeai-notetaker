"""Single home for every Google Meet DOM selector (PR 1 spike).

Policy: aria-label / role based only, `en-US` locale forced. A Meet UI
change must be a one-file fix — edit this file, nothing else. Never target
CSS classnames; Meet A/B experiments rename them constantly.

Every function takes a Playwright `Page` and returns the first matching
visible element, or `None` when nothing matched within the timeout.
Callers decide what "not found" means (usually: save a debug screenshot,
log a warning, keep going).

Note: this module is imported as `bot.selectors` (never a flat script
import) so it cannot shadow Python's stdlib `selectors` module, which
Playwright/asyncio need.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

Role = Literal["button", "textbox"]
RoleQuery = tuple[Role, re.Pattern[str]]

_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile("Join now.*", re.IGNORECASE)),
    ("button", re.compile("Ask to join.*", re.IGNORECASE)),
)
_MICROPHONE: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(turn on|turn off) microphone.*", re.IGNORECASE)),
)
_CAMERA: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(turn on|turn off) camera.*", re.IGNORECASE)),
)
_LEAVE_CALL: tuple[RoleQuery, ...] = (("button", re.compile("Leave call.*", re.IGNORECASE)),)
_NAME_INPUT: tuple[RoleQuery, ...] = (("textbox", re.compile(r"your name.*", re.IGNORECASE)),)
_KNOCKING: re.Pattern[str] = re.compile("Asking to be let in.*", re.IGNORECASE)
_CALL_ENDED: re.Pattern[str] = re.compile(
    r"meeting has ended.*|you[\u2019']?ve left the meeting.*|you left the meeting.*",
    re.IGNORECASE,
)
_REMOVED: re.Pattern[str] = re.compile(
    r"removed from the meeting.*|removed you.*",
    re.IGNORECASE,
)
_JOIN_BLOCKED: re.Pattern[str] = re.compile(
    r"you can[\u2019']?t join this video call.*",
    re.IGNORECASE,
)


def _role_locators(page: Page, queries: tuple[RoleQuery, ...]) -> list[Locator]:
    return [page.get_by_role(role, name=name) for role, name in queries]


def _text_locators(page: Page, patterns: tuple[re.Pattern[str], ...]) -> list[Locator]:
    return [page.get_by_text(pattern) for pattern in patterns]


def _first_visible(page: Page, locators: list[Locator], timeout_ms: int = 1000) -> Locator | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for locator in locators:
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def join_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _JOIN), timeout_ms)


def microphone_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _MICROPHONE), timeout_ms)


def camera_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _CAMERA), timeout_ms)


def leave_call_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _LEAVE_CALL), timeout_ms)


def name_input(page: Page, timeout_ms: int = 1000) -> Locator | None:
    locators = _role_locators(page, _NAME_INPUT)
    locators.append(page.locator('input[aria-label*="name" i]'))
    return _first_visible(page, locators, timeout_ms)


def knocking_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_KNOCKING,)), timeout_ms)


def call_ended_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_CALL_ENDED,)), timeout_ms)


def removed_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_REMOVED,)), timeout_ms)


def join_blocked_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_JOIN_BLOCKED,)), timeout_ms)
