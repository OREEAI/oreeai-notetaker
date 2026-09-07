"""Consent: fixed bot identity, ack gate, and the in-call announcement (PR 3).

Every participant must be able to see, from inside the meeting, that a
recording is happening:

- Fixed name: the bot's identity is hard-coded to ``Oree Notetaker``. In
  anonymous mode it types that name into the green room; in authenticated
  mode the dedicated account's display name *is* the consent signal, so no
  typing happens and the name cannot diverge. ``BOT_NAME`` is deprecated:
  any other value logs a warning naming both values, then the hard-coded
  name is used.
- ``CONSENT_ACK`` gate: the bot refuses to start (exit 6) unless the env
  var is explicitly true. Checked before any browser work.
- Chat announcement: once recording starts, the exact consent message is
  posted to the in-meeting chat. Best-effort with distinct log lines for
  success / failure / element-not-found; recording never stops for chat.

This module owns the consent *policy*; the click-through lifecycle that
calls into it stays in :mod:`bot.listeners` / :mod:`bot.join_meet`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from bot import selectors
from bot.humanize import click_like_human, move_to, pause_between_actions, type_text
from bot.listeners import debug_screenshot

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

logger = logging.getLogger("oreeai.bot.consent")

# The consent identity. Authenticated-mode runs require a dedicated Google
# account with this display name; anonymous runs type it in the green room.
BOT_NAME_FIXED = "Oree Notetaker"

# Exact in-call announcement. Wording is a product decision (PR 3 chunk);
# do not rephrase without sign-off.
CONSENT_MESSAGE = (
    "Hi, this is Oree Notetaker. This call is being recorded and transcribed "
    "for note-taking. Let me know if you'd like me to leave."
)

# Per-element finder budget for the chat UI (covers the post-admit race).
# On a miss the announcement is skipped with a warning; recording continues.
CHAT_FIND_TIMEOUT_S = 10.0

# Chat typing runs faster than the green-room name (the message should be
# readable within ~10 s of recording starting). Green-room pacing unchanged.
CHAT_TYPE_DELAY_MS = (15.0, 45.0)

_PRODUCTION = "production"

ChatSelectorQuery = Callable[..., "Locator | None"]


class ChatSelectorSet(Protocol):
    """The subset of :mod:`bot.selectors` the announcement needs."""

    chat_open_button: ChatSelectorQuery
    chat_message_box: ChatSelectorQuery
    chat_send_button: ChatSelectorQuery


_DEFAULT_CHAT_SELECTORS: ChatSelectorSet = selectors


def resolve_bot_name() -> str:
    """Return the hard-coded consent identity name, warning on legacy ``BOT_NAME``."""
    raw = os.environ.get("BOT_NAME", "").strip()
    if not raw or raw == BOT_NAME_FIXED:
        return BOT_NAME_FIXED
    logger.warning(
        "BOT_NAME=%r is deprecated; the bot name is hard-coded to %r for consent "
        "visibility - proceeding with %r",
        raw,
        BOT_NAME_FIXED,
        BOT_NAME_FIXED,
    )
    return BOT_NAME_FIXED


def consent_granted() -> bool:
    """Whether ``CONSENT_ACK`` explicitly acknowledges recording consent."""
    return os.environ.get("CONSENT_ACK", "").strip().lower() in ("1", "true", "yes")


def environment() -> str:
    """Deployment environment name (lowercased); unset means local."""
    return os.environ.get("ENVIRONMENT", "local").strip().lower() or "local"


# Mirrors the signed-in avatar's accessible-name form in bot/selectors.py:
# "Google Account: Name (email)".
_GOOGLE_ACCOUNT_LABEL = re.compile(r"google\s+account\s*:\s*(.+)$", re.IGNORECASE)


def account_display_name(page: Page, timeout_ms: int = 0) -> str | None:
    """The signed-in account's display name, or None when not determinable.

    Reads the same avatar control the session gate verified; call it only
    after a positive session check. The accessible name may live in an
    ``aria-label`` or in the control's inner text (the live
    myaccount.google.com avatar uses text — same precedence as
    ``states._locator_detail``). ``timeout_ms=0`` means a single pass.
    """
    avatar = selectors.signed_in_indicator(page, timeout_ms=timeout_ms)
    if avatar is None:
        return None
    label = avatar.get_attribute("aria-label") or ""
    if not label:
        try:
            label = avatar.inner_text()
        except Exception:
            label = ""
    match = _GOOGLE_ACCOUNT_LABEL.search(label)
    if match is None:
        return None
    rest = match.group(1).strip()
    if rest.endswith(")"):
        open_paren = rest.rfind("(")
        if open_paren != -1:
            rest = rest[:open_paren].strip()
    return rest or None


def verify_consent_identity(display_name: str | None, *, environment: str) -> str | None:
    """Return a fatal reason when the signed-in identity breaks the consent contract.

    Production is strict: the dedicated ``Oree Notetaker`` account *is* the
    consent signal, so a different (or undeterminable) display name must not
    join. Other environments warn and proceed (any account is fine for
    development and staging).
    """
    strict = environment == _PRODUCTION
    if display_name == BOT_NAME_FIXED:
        return None
    if display_name is None:
        detail = (
            "could not determine the signed-in account display name; "
            f"the consent identity must be the dedicated '{BOT_NAME_FIXED}' account"
        )
    else:
        detail = (
            f"signed in as '{display_name}'; the consent identity must be the dedicated "
            f"'{BOT_NAME_FIXED}' account (run make bot-login with the dedicated account)"
        )
    if strict:
        return detail
    logger.warning("%s (warning only because ENVIRONMENT=%s)", detail, environment or "local")
    return None


def _chat_not_found(what: str, page: Page, find_timeout_s: float) -> None:
    debug_screenshot(page, f"chat {what} not found")
    logger.warning(
        "chat not available (%s not found within %s s); continuing without the "
        "in-call announcement (recording unaffected)",
        what,
        find_timeout_s,
    )


def post_chat_announcement(
    page: Page,
    *,
    selector_set: ChatSelectorSet = _DEFAULT_CHAT_SELECTORS,
    find_timeout_s: float = CHAT_FIND_TIMEOUT_S,
    message: str = CONSENT_MESSAGE,
) -> bool:
    """Post the consent message to the in-meeting chat. Never raises.

    Returns True when the message was posted. Any miss or error is logged
    (success / failure / element-not-found are distinct lines) and the call
    keeps recording; the visible bot name remains the fallback signal.
    """
    timeout_ms = int(find_timeout_s * 1000)
    toggle = selector_set.chat_open_button(page, timeout_ms=timeout_ms)
    if toggle is None:
        _chat_not_found("open-chat control", page, find_timeout_s)
        return False
    try:
        move_to(page, toggle)
        click_like_human(toggle)
        pause_between_actions(page)
        box = selector_set.chat_message_box(page, timeout_ms=timeout_ms)
        if box is None:
            _chat_not_found("message box", page, find_timeout_s)
            return False
        type_text(page, box, message, char_delay_ms=CHAT_TYPE_DELAY_MS)
        send = selector_set.chat_send_button(page, timeout_ms=0)
        if send is not None:
            move_to(page, send)
            click_like_human(send)
        else:
            box.press("Enter")
    except Exception as exc:
        debug_screenshot(page, "chat announcement interaction failed")
        logger.warning(
            "consent chat announcement failed: %s; continuing (recording unaffected)", exc
        )
        return False
    logger.info("consent announcement posted to chat")
    return True
