"""Single home for every Zoom web-client DOM selector (Z1 spike).

Policy: aria-label / role / visible-text based only, ``en-US`` locale forced.
A Zoom web-client UI change must be a one-file fix — edit this file, nothing
else. Never target CSS classnames; the web client is a Vue app whose scoped
classnames change with every build.

The current web client renders its pre-join and in-call UI inside a
same-origin iframe (``id="webclient"``) on ``app.zoom.us``. Every selector
function therefore resolves that frame first and falls back to the page
itself (the landing page, and older builds that render inline). Patterns
below are pinned to the Z1 live join session (2026-10-05) and to the
snapshots in ``tests/bot/fixtures/zoom/``.

Every function takes a Playwright ``Page`` and returns the first matching
visible element, or ``None`` when nothing matched within the timeout.
Callers decide what "not found" means (usually: save a debug screenshot,
log a warning, keep going).
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from playwright.sync_api import FrameLocator, Locator, Page

Role = Literal["button", "textbox", "link"]
RoleQuery = tuple[Role, re.Pattern[str]]

# The web client iframe (live 2026-10-05 evidence: id="webclient",
# class="pwa-webclient__iframe"); the src fallback covers variants.
_CLIENT_FRAME_SELECTORS: tuple[str, ...] = ("iframe#webclient", 'iframe[src*="/wc/"]')

# Landing page ("/j/<id>"): the human path into the web client. The current
# build presents a *button* labelled "Join from browser" next to "Join from
# Zoom Workplace app"; older builds used a plain link "Join from your
# browser". Both roles are queried for both wordings.
_BROWSER_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"join from (your )?browser.*", re.IGNORECASE)),
    ("link", re.compile(r"join from (your )?browser.*", re.IGNORECASE)),
)
# Web-client pre-join "Join" control: a plain button whose text is "Join".
_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"^\s*join\s*$", re.IGNORECASE)),
    ("button", re.compile(r"^\s*join meeting\s*$", re.IGNORECASE)),
)
# The live pre-join name input carries no accessible name (a class-only
# label), so callers fall back to the first visible textbox in the client
# frame. The named query stays first for builds that do expose it.
_NAME_INPUT: tuple[RoleQuery, ...] = (("textbox", re.compile(r"your name.*", re.IGNORECASE)),)
# Pre-join mic/camera labels are the *action* ("Mute", "Stop Video");  the
# live in-call toolbar uses lowercase action labels ("mute my microphone").
_MICROPHONE: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(mute|unmute) my microphone.*", re.IGNORECASE)),
    ("button", re.compile(r"^\s*(mute|unmute)\s*$", re.IGNORECASE)),
    ("button", re.compile(r"(turn on|turn off) microphone.*", re.IGNORECASE)),
)
_CAMERA: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"(start|stop) (my )?video.*", re.IGNORECASE)),
    ("button", re.compile(r"^\s*video\s*$", re.IGNORECASE)),
    ("button", re.compile(r"(turn on|turn off) camera.*", re.IGNORECASE)),
)
# Post-join audio dialog (not shown when the account auto-joins computer
# audio; kept for configurations that do show it).
_AUDIO_JOIN: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"join (audio )?(by|with) computer( audio)?.*", re.IGNORECASE)),
    ("button", re.compile(r"join audio.*", re.IGNORECASE)),
    ("button", re.compile(r"use computer audio.*", re.IGNORECASE)),
)
_LEAVE: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"^\s*leave( meeting)?\s*$", re.IGNORECASE)),
)
# Live in-call control: aria "open the participants list pane,[2] particpants"
# (Zoom's typo) with visible text "2\nParticipants".
_PARTICIPANTS: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"open the participants.*", re.IGNORECASE)),
    ("button", re.compile(r"participants.*", re.IGNORECASE)),
    ("button", re.compile(r"show participants.*", re.IGNORECASE)),
)
_COOKIE_ACCEPT: tuple[RoleQuery, ...] = (
    ("button", re.compile(r"^\s*accept cookies\s*$", re.IGNORECASE)),
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


def _root(page: Page) -> Page | FrameLocator:
    """The frame root carrying the web-client UI (the page on landing)."""
    for selector in _CLIENT_FRAME_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return page.frame_locator(selector).first
        except Exception:
            continue
    return page


def _role_locators(root: Page | FrameLocator, queries: tuple[RoleQuery, ...]) -> list[Locator]:
    return [root.get_by_role(role, name=name) for role, name in queries]


def _text_locators(
    root: Page | FrameLocator, patterns: tuple[re.Pattern[str], ...]
) -> list[Locator]:
    return [root.get_by_text(pattern) for pattern in patterns]


def _first_visible(page: Page, locators: list[Locator], timeout_ms: int = 1000) -> Locator | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for locator in locators:
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def client_frame_present(page: Page, timeout_ms: int = 0) -> bool:
    """Whether the web-client iframe exists (used to confirm navigation)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for selector in _CLIENT_FRAME_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def browser_join_link(page: Page, timeout_ms: int = 1000) -> Locator | None:
    """The "/j" landing page's click-through into the web client."""
    return _first_visible(page, _role_locators(page, _BROWSER_JOIN), timeout_ms)


def join_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    """The web-client pre-join page's Join control."""
    return _first_visible(page, _role_locators(_root(page), _JOIN), timeout_ms)


def name_input(page: Page, timeout_ms: int = 1000) -> Locator | None:
    root = _root(page)
    locators = _role_locators(root, _NAME_INPUT)
    # Live evidence (2026-10-05): the pre-join input has no accessible name;
    # it is the only visible textbox in the client frame.
    locators.append(root.get_by_role("textbox"))
    return _first_visible(page, locators, timeout_ms)


def microphone_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(_root(page), _MICROPHONE), timeout_ms)


def camera_toggle(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(_root(page), _CAMERA), timeout_ms)


def audio_join_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(_root(page), _AUDIO_JOIN), timeout_ms)


def leave_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(_root(page), _LEAVE), timeout_ms)


def waiting_room_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_WAITING_ROOM,)), timeout_ms)


def meeting_not_started_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_MEETING_NOT_STARTED,)), timeout_ms)


def removed_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_REMOVED,)), timeout_ms)


def call_ended_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_CALL_ENDED,)), timeout_ms)


def participant_count_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(_root(page), _PARTICIPANTS), timeout_ms)


def alone_hint(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_ALONE_HINT,)), timeout_ms)


def cookie_accept_button(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _role_locators(page, _COOKIE_ACCEPT), timeout_ms)


def passcode_input(page: Page, timeout_ms: int = 1000) -> Locator | None:
    root = _root(page)
    locators = _role_locators(root, _PASSCODE_INPUT)
    locators.append(root.locator('input[aria-label*="passcode" i]'))
    locators.append(root.locator('input[placeholder*="passcode" i]'))
    return _first_visible(page, locators, timeout_ms)


def passcode_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_PASSCODE_REQUIRED,)), timeout_ms)


def sign_in_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_SIGN_IN_REQUIRED,)), timeout_ms)


def only_authenticated_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_ONLY_AUTHENTICATED,)), timeout_ms)


def desktop_app_required_indicator(page: Page, timeout_ms: int = 1000) -> Locator | None:
    return _first_visible(page, _text_locators(_root(page), (_DESKTOP_APP_REQUIRED,)), timeout_ms)
