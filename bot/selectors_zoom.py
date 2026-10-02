"""Single home for every Zoom web-client DOM selector (Z1 spike).

Policy: aria-label / role / visible-text based only, ``en-US`` locale forced.
A Zoom web-client UI change must be a one-file fix — edit this file, nothing
else. Never target CSS classnames; the web client is a Vue app whose scoped
classnames change with every build.

Every function takes a Playwright ``Page`` and returns the first matching
visible element, or ``None`` when nothing matched within the timeout.
Callers decide what "not found" means (usually: save a debug screenshot,
log a warning, keep going).

The patterns below cover the guest web-client flow: the ``/j/<id>`` landing
page ("Join from your browser" click-through), the web-client pre-join page
(name + media toggles + Join), the post-join audio dialog, the in-call
toolbar, and the wall notices the spike fails fast on. They are pinned to
the DOM snapshots in ``tests/bot/fixtures/zoom/``; when the live Z1 join
session corrects a pattern, update the fixture and this file together.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

Role = Literal["button", "textbox", "link"]
RoleQuery = tuple[Role, re.Pattern[str]]

# Landing page (/j/<id>): the human path into the web client. The control has
# appeared as both a link and (on some variants) a button; both are queried.
_BROWSER_JOIN: tuple[RoleQuery, ...] = (
    ("link", re.compile(r"join from your browser.*", re.IGNORECASE)),
    ("link", re.compile(r"join from browser.*", re.IGNORECASE)),
    ("button", re.compile(r"join from your browser.*", re.IGNORECASE)),
)
# Web-client pre-join "Join" control. Anchored so it cannot catch link/button
# labels such as "Join from your browser" or "Join with computer audio".
_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"^\s*join\s*$", re.IGNORECASE)),
    ("button", re.compile(r"^\s*join meeting\s*$", re.IGNORECASE)),
)
_NAME_INPUT: tuple[RoleQuery, ...] = (("textbox", re.compile(r"your name.*", re.IGNORECASE)),)
_MICROPHONE: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(mute|unmute) my microphone.*", re.IGNORECASE)),
    ("button", re.compile(r"(turn on|turn off) microphone.*", re.IGNORECASE)),
)
_CAMERA: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(start|stop) my video.*", re.IGNORECASE)),
    ("button", re.compile(r"(turn on|turn off) camera.*", re.IGNORECASE)),
)
# Post-join audio dialog. The exact button has shipped as "Join with Computer
# Audio" and "Join Audio by Computer"; the generic fallbacks cover variants.
_AUDIO_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"join (audio )?(by|with) computer( audio)?.*", re.IGNORECASE)),
    ("button", re.compile(r"join audio.*", re.IGNORECASE)),
    ("button", re.compile(r"use computer audio.*", re.IGNORECASE)),
)
_LEAVE: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"^\s*leave( meeting)?\s*$", re.IGNORECASE)),
)
_PARTICIPANTS: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"participants.*", re.IGNORECASE)),
    ("button", re.compile(r"show participants.*", re.IGNORECASE)),
)
# Waiting-room vs. meeting-not-started are deliberately disjoint patterns:
# the queue contract requires two separate predicates, never one heuristic.
_WAITING_ROOM: re.Pattern[str] = re.compile(
    r"waiting for the host to let you in.*|host will let you in.*|"
    r"you are in the waiting room.*|please wait until the host.*",
    re.IGNORECASE,
)
_MEETING_NOT_STARTED: re.Pattern[str] = re.compile(
    r"meeting has not started.*|hasn[\u2019']?t started.*|"
    r"wait(?:ing)? for the host to start.*|host has not (?:yet )?started.*",
    re.IGNORECASE,
)
_REMOVED: re.Pattern[str] = re.compile(
    r"removed (you )?from (this |the )?meeting.*|host.*removed you.*|"
    r"you have been removed.*",
    re.IGNORECASE,
)
_CALL_ENDED: re.Pattern[str] = re.compile(
    r"meeting has been ended.*|has ended by (the )?host.*|host ended the meeting.*|"
    r"meeting ended.*",
    re.IGNORECASE,
)
# Zoom's alone cue inside the participant panel. The reliable signal is the
# participant count (states_zoom); this text is a secondary confirmation.
_ALONE_HINT: re.Pattern[str] = re.compile(
    r"you are the only (one|participant).*|no one else is here.*|only you are in.*",
    re.IGNORECASE,
)
_PASSCODE_INPUT: tuple[RoleQuery, ...] = (
    ("textbox", re.compile(r"(meeting |enter )?passcode.*", re.IGNORECASE)),
)
_PASSCODE_REQUIRED: re.Pattern[str] = re.compile(
    r"enter (the )?(meeting )?passcode.*|passcode is (required|incorrect).*|"
    r"incorrect passcode.*",
    re.IGNORECASE,
)
# Guest-CAPTCHA wall: flagged guests are pushed to "sign in to join". The
# bare header "Sign In" link on a normal /j page must never match, hence the
# required "to join"/"to the meeting" continuation.
_SIGN_IN_REQUIRED: re.Pattern[str] = re.compile(
    r"sign in to join.*|sign in to (the )?meeting.*|please sign in.*join.*",
    re.IGNORECASE,
)
_ONLY_AUTHENTICATED: re.Pattern[str] = re.compile(
    r"only authenticated users.*|only for authenticated users.*|"
    r"requires? authentication.*|authenticated users can join.*",
    re.IGNORECASE,
)
# E2EE meetings and hosts that require the desktop client both surface as
# "no web-client path" notices; both are fast fails with an honest reason.
_DESKTOP_APP_REQUIRED: re.Pattern[str] = re.compile(
    r"requires? the (zoom )?(desktop )?(app|client).*|"
    r"only (available|supported) in the (zoom )?(desktop )?(app|client).*|"
    r"end-to-end encryption.*|e2ee.*",
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


def browser_join_link(page: Page, timeout_ms: int = 1000) -> Locator | None:
    """The "/j" landing page's click-through into the web client."""
    return _first_visible(page, _role_locators(page, _BROWSER_JOIN), timeout_ms)


def join_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    """The web-client pre-join page's Join control."""
    return _first_visible(page, _role_locators(page, _JOIN), timeout_ms)


def name_input(page: Page, timeout_ms: int = 1000) -> Locator | None:
    locators = _role_locators(page, _NAME_INPUT)
    locators.append(page.locator('input[aria-label*="name" i]'))
    locators.append(page.locator('input[placeholder*="name" i]'))
    return _first_visible(page, locators, timeout_ms)


def microphone_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _MICROPHONE), timeout_ms)


def camera_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _CAMERA), timeout_ms)


def audio_join_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _AUDIO_JOIN), timeout_ms)


def leave_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _LEAVE), timeout_ms)


def waiting_room_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_WAITING_ROOM,)), timeout_ms)


def meeting_not_started_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_MEETING_NOT_STARTED,)), timeout_ms)


def removed_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_REMOVED,)), timeout_ms)


def call_ended_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_CALL_ENDED,)), timeout_ms)


def participant_count_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _PARTICIPANTS), timeout_ms)


def alone_hint(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_ALONE_HINT,)), timeout_ms)


def passcode_input(page: Page, timeout_ms: int = 1000) -> Locator | None:
    locators = _role_locators(page, _PASSCODE_INPUT)
    locators.append(page.locator('input[aria-label*="passcode" i]'))
    locators.append(page.locator('input[placeholder*="passcode" i]'))
    return _first_visible(page, locators, timeout_ms)


def passcode_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_PASSCODE_REQUIRED,)), timeout_ms)


def sign_in_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_SIGN_IN_REQUIRED,)), timeout_ms)


def only_authenticated_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_ONLY_AUTHENTICATED,)), timeout_ms)


def desktop_app_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(page, (_DESKTOP_APP_REQUIRED,)), timeout_ms)
