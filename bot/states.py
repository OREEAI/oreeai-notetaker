"""Pure Google Meet call-state predicates.

Each predicate takes a Playwright page and a selector set, defaulting to the
shared selectors in :mod:`bot.selectors`. The predicates perform single-shot
DOM checks; they never wait, click, navigate, start recording, or log.
Stateful timing, transitions, and side effects belong in :mod:`bot.listeners`.

The returned detail is a short human-readable diagnostic string. It may
contain visible Meet UI text, but never audio bytes or filesystem paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from bot import selectors

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

SelectorQuery = Callable[..., "Locator | None"]


class SelectorSet(Protocol):
    leave_call_button: SelectorQuery
    knocking_indicator: SelectorQuery
    name_input: SelectorQuery
    join_button: SelectorQuery
    call_ended_indicator: SelectorQuery
    removed_indicator: SelectorQuery
    participant_count_button: SelectorQuery
    alone_hint: SelectorQuery
    signed_in_indicator: SelectorQuery


_DEFAULT_SELECTOR_SET: SelectorSet = selectors

_POLL_TIMEOUT_MS = 0
_DETAIL_CHARS = 120
_PARTICIPANT_COUNT_RE = re.compile(r"people\s*\((\d+)\)", re.IGNORECASE)


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


def is_in_waiting_room(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the page is still waiting for admission.

    The waiting room includes both the pre-join green room and the
    post-click admission-wait screen. An admitted call is never a waiting
    room, even if one of these controls is unexpectedly still visible.
    """
    if selector_set.leave_call_button(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return False, "leave control visible; not in waiting room"

    knocking = selector_set.knocking_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if knocking is not None:
        return True, f"waiting for admission: {_locator_detail(knocking)}"

    name_field = selector_set.name_input(page, timeout_ms=_POLL_TIMEOUT_MS)
    if name_field is not None:
        return True, f"pre-join green room: {_locator_detail(name_field)}"

    join = selector_set.join_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if join is not None:
        return True, f"pre-join green room: {_locator_detail(join)}"

    return False, "no green-room or admission-wait controls visible"


def is_admitted(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether the bot has entered the call."""
    if selector_set.leave_call_button(page, timeout_ms=_POLL_TIMEOUT_MS) is not None:
        return True, "in call: leave control visible"
    return False, "leave control absent"


def is_removed(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether Meet reports that the bot was removed from the call."""
    removed = selector_set.removed_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if removed is not None:
        return True, f"removed from call: {_locator_detail(removed)}"
    return False, "no removal notice visible"


def is_call_ended(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether Meet reports that the call has ended."""
    ended = selector_set.call_ended_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if ended is not None:
        return True, f"call ended: {_locator_detail(ended)}"
    return False, "no call-ended notice visible"


def is_signed_in(page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET) -> tuple[bool, str]:
    """Whether the page shows an active Google session.

    The indicator is the signed-in account avatar ("Google Account: Name
    (email)"), not bare "Google Account" text: Chrome's first-run promo copy
    contains the bare text with no session behind it.
    """
    signed_in = selector_set.signed_in_indicator(page, timeout_ms=_POLL_TIMEOUT_MS)
    if signed_in is not None:
        return True, f"google session active: {_locator_detail(signed_in)}"
    return False, "no signed-in session visible"


def participant_count(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[int | None, str]:
    """Number of participants shown by Meet, including the bot.

    ``None`` means the count could not be determined from the visible page.
    Callers must distinguish unknown from empty: an unknown count is not
    evidence that the room is empty.
    """
    if selector_set.leave_call_button(page, timeout_ms=_POLL_TIMEOUT_MS) is None:
        return None, "not in a call"

    button = selector_set.participant_count_button(page, timeout_ms=_POLL_TIMEOUT_MS)
    if button is None:
        return None, "participant-count control absent"

    label = button.get_attribute("aria-label") or ""
    text = _safe_inner_text(button)
    match = _PARTICIPANT_COUNT_RE.search(label) or _PARTICIPANT_COUNT_RE.search(text)
    if match is None:
        return None, f"participant count unavailable ({_locator_detail(button)})"

    count = int(match.group(1))
    noun = "participant" if count == 1 else "participants"
    return count, f"{count} {noun} in call"


def bot_alone_in_call(
    page: Page, selector_set: SelectorSet = _DEFAULT_SELECTOR_SET
) -> tuple[bool, str]:
    """Whether the available signals show only the bot left in the call."""
    if selector_set.leave_call_button(page, timeout_ms=_POLL_TIMEOUT_MS) is None:
        return False, "not in a call"

    count, count_detail = participant_count(page)
    hint = selector_set.alone_hint(page, timeout_ms=_POLL_TIMEOUT_MS)
    if count == 1:
        if hint is not None:
            return True, f"only the bot remains ({count_detail}; {_locator_detail(hint)})"
        return True, f"only the bot remains ({count_detail})"
    if count is not None and count > 1:
        return False, f"other participants remain ({count_detail})"

    # The count is unavailable or impossible (for example zero while the bot
    # is visibly in the call). Do not infer emptiness without Meet's own
    # alone-room text.
    if hint is not None:
        return True, f"only the bot remains ({_locator_detail(hint)})"
    return False, f"not enough evidence the bot is alone ({count_detail})"
