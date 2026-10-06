"""Pure Zoom web-client call-state predicates.

Each predicate takes a Playwright page and a selector set, defaulting to the
shared selectors in :mod:`bot.selectors_zoom`. The predicates perform
single-shot DOM checks; they never wait, click, navigate, start recording,
or log. Stateful timing, transitions, and side effects belong in
:mod:`bot.listeners_zoom`.

Waiting-room and meeting-not-started are separate predicates by contract:
Zoom distinguishes "waiting to be let in" from "the host has not started
the meeting yet", and the Z2 lifecycle must be able to tell them apart
without string heuristics across both.

The returned detail is a short human-readable diagnostic string. It may
contain visible Zoom UI text, but never audio bytes or filesystem paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from bot import selectors_zoom

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

SelectorQuery = Callable[..., "Locator | None"]


class SelectorSet(Protocol):
    browser_join_link: SelectorQuery
    join_button: SelectorQuery
    name_input: SelectorQuery
    microphone_toggle: SelectorQuery
    camera_toggle: SelectorQuery
    audio_join_button: SelectorQuery
    leave_button: SelectorQuery
    end_button: SelectorQuery
    waiting_room_indicator: SelectorQuery
    meeting_not_started_indicator: SelectorQuery
    removed_indicator: SelectorQuery
    call_ended_indicator: SelectorQuery
    participant_count_button: SelectorQuery
    alone_hint: SelectorQuery
    passcode_input: SelectorQuery
    passcode_required_indicator: SelectorQuery
    sign_in_required_indicator: SelectorQuery
    only_authenticated_indicator: SelectorQuery
    desktop_app_required_indicator: SelectorQuery


_DEFAULT_SELECTOR_SET: SelectorSet = selectors_zoom

_POLL_TIMEOUT_MS = 0
_DETAIL_CHARS = 120
# Live in-call shapes (2026-10-05): aria "open the participants list pane,[2]
# particpants" (Zoom's spelling) and visible text "2\nParticipants".
_PARTICIPANT_BRACKET_RE = re.compile(r"\[(\d{1,3})\]")
_PARTICIPANT_COUNT_RE = re.compile(r"participants?\s*\(?(\d+)\)?", re.IGNORECASE)
_PARTICIPANT_LEADING_RE = re.compile(r"^\s*(\d{1,3})\s+participants?\b", re.IGNORECASE)
# Zoom's toolbar chip may render the bare count next to the control label; a
# standalone number in the accessible name is accepted like Meet's chip.
_BARE_COUNT_RE = re.compile(r"^\s*\(?\s*(\d{1,3})\s*\)?\s*$")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str) -> str:
    return _normalize(text)[:_DETAIL_CHARS]


def _safe_inner_text(locator: Locator) -> str:
    try:
        return locator.inner_text()
    except Exception:
        return ""


def _locator_detail(locator: Locator) -> str:
    label = locator.get_attribute("aria-label") or ""
    return _clip(label or _safe_inner_text(locator) or "matching control")


def in_call_controls(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Zoom's in-call toolbar is visible.

    The toolbar shows "Leave" for regular participants; when the bot has
    been promoted to host (live 2026-10-06: the original host left), the
    control becomes "End". Both are in-call evidence.
    """
    leave = selector_set.leave_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if leave is not None:
        return True, f"in call: {_locator_detail(leave)}"
    end = selector_set.end_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if end is not None:
        return True, f"in call: {_locator_detail(end)}"
    return False, "leave/end control absent"


def is_prejoin(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether the web client is on its pre-join page.

    A visible in-call toolbar means the bot is already in the call; a visible
    waiting/not-started notice means the join was already submitted. Only
    the name field / Join control mark the actual pre-join form.
    """
    controls, controls_detail = in_call_controls(page, selector_set)
    if controls:
        return False, f"in-call controls visible ({controls_detail})"
    if selector_set.waiting_room_indicator(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return False, "waiting-room notice visible"
    if selector_set.meeting_not_started_indicator(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return False, "meeting-not-started notice visible"

    name_field = selector_set.name_input(page, timeout_ms=_POLL_TIMEOUT_MS)
    if name_field is not None:
        return True, f"pre-join form: {_locator_detail(name_field)}"
    join = selector_set.join_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if join is not None:
        return True, f"pre-join form: {_locator_detail(join)}"
    return False, "no pre-join controls visible"


def is_in_waiting_room(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Zoom is holding the bot in the waiting room."""
    notice = selector_set.waiting_room_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"waiting for admission: {_locator_detail(notice)}"
    return False, "no waiting-room notice visible"


def is_meeting_not_started(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Zoom reports that the host has not started the meeting yet."""
    notice = selector_set.meeting_not_started_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"meeting not started: {_locator_detail(notice)}"
    return False, "no meeting-not-started notice visible"


def is_admitted(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether the bot has entered the call.

    The waiting-room and meeting-not-started notices override the in-call
    controls: Zoom's waiting pages can render their own leave control, which
    must never read as admission (same rule as Meet's knock page).
    """
    if selector_set.waiting_room_indicator(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return False, "waiting-room notice overrides the in-call controls"
    if selector_set.meeting_not_started_indicator(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return False, "meeting-not-started notice overrides the in-call controls"
    controls, controls_detail = in_call_controls(page, selector_set)
    if controls:
        return True, controls_detail
    return False, "leave/end control absent"


def is_removed(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether Zoom reports that the bot was removed from the meeting."""
    removed = selector_set.removed_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if removed is not None:
        return True, f"removed from meeting: {_locator_detail(removed)}"
    return False, "no removal notice visible"


def is_call_ended(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Zoom reports that the meeting has ended."""
    ended = selector_set.call_ended_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if ended is not None:
        return True, f"call ended: {_locator_detail(ended)}"
    return False, "no call-ended notice visible"


def participant_count(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[int | None, str]:
    """Number of participants shown by Zoom, including the bot.

    ``None`` means the count could not be determined from the visible page.
    Callers must distinguish unknown from empty: an unknown count is not
    evidence that the room is empty.
    """
    controls, _ = in_call_controls(page, selector_set)
    if not controls:
        return None, "not in a call"

    button = selector_set.participant_count_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if button is None:
        return None, "participant-count control absent"

    label = button.get_attribute("aria-label") or ""
    text = _safe_inner_text(button)
    count_text = None
    for source in (label, text):
        for pattern in (_PARTICIPANT_BRACKET_RE, _PARTICIPANT_COUNT_RE, _PARTICIPANT_LEADING_RE):
            match = pattern.search(source)
            if match is not None:
                count_text = match.group(1)
                break
        if count_text is not None:
            break
    if count_text is None:
        bare = _BARE_COUNT_RE.match(label) or _BARE_COUNT_RE.match(text)
        count_text = bare.group(1) if bare is not None else None
    if count_text is None:
        return None, f"participant count unavailable ({_locator_detail(button)})"

    count = int(count_text)
    noun = "participant" if count == 1 else "participants"
    return count, f"{count} {noun} in call"


def bot_alone_in_call(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the available signals show only the bot left in the call."""
    controls, _ = in_call_controls(page, selector_set)
    if not controls:
        return False, "not in a call"

    count, count_detail = participant_count(page, selector_set)
    hint = selector_set.alone_hint(page, timeout_ms=_POLL_TIMEOUT_MS)
    if count == 1:
        if hint is not None:
            return True, f"only the bot remains ({count_detail}; {_locator_detail(hint)})"
        return True, f"only the bot remains ({count_detail})"
    if count is not None and count > 1:
        return False, f"other participants remain ({count_detail})"

    # The count is unavailable. Do not infer emptiness without Zoom's own
    # alone text; Z2 owns the alone/empty-room grace logic that consumes this.
    if hint is not None:
        return True, f"only the bot remains ({_locator_detail(hint)})"
    return False, f"not enough evidence the bot is alone ({count_detail})"


def is_passcode_screen(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Zoom is asking for a passcode the bot does not have."""
    field = selector_set.passcode_input(page, timeout_ms=_POLL_TIMEOUT_MS)
    if field is not None:
        return True, f"passcode required: {_locator_detail(field)}"
    notice = selector_set.passcode_required_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"passcode required: {_locator_detail(notice)}"
    return False, "no passcode prompt visible"


def is_sign_in_required(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the guest join was walled with Zoom's "sign in to join" screen."""
    notice = selector_set.sign_in_required_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"sign-in required: {_locator_detail(notice)}"
    return False, "no sign-in wall visible"


def is_only_authenticated(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the meeting admits authenticated users only."""
    notice = selector_set.only_authenticated_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"authenticated-only meeting: {_locator_detail(notice)}"
    return False, "no authenticated-only notice visible"


def is_desktop_app_required(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the meeting has no web-client path (E2EE / desktop app required)."""
    notice = selector_set.desktop_app_required_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if notice is not None:
        return True, f"desktop app required: {_locator_detail(notice)}"
    return False, "no desktop-app-required notice visible"
