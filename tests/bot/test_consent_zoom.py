"""Unit tests for the Z3 Zoom chat announcement (shared consent flow).

Mirrors the Meet chat block in ``test_consent.py`` against the live Zoom
web-client shape captured 2026-10-08 (``plans/handoffs/zoom-evidence/z3_chat``):
a footer chat control with aria "open the chat panel", a bare
``contenteditable`` composer, and an aria "send" control. Also pins the two
verified fallbacks Zoom's client needs — DOM click when a trusted click
cannot land and ``fill`` when the editor ignores key events. Browser-free.
"""

import logging
from pathlib import Path

import pytest

from bot import consent, selectors_zoom
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures" / "zoom"

MESSAGE = (
    "Hi, this is Oree Notetaker. This call is being recorded and transcribed "
    "for note-taking. Let me know if you'd like me to leave."
)


def page(name: str) -> FakePage:
    return FakePage.from_fixture(FIXTURES / name)


def test_zoom_selectors_satisfy_chat_selector_set() -> None:
    """Zoom's module is structurally a ChatSelectorSet; no consent.py fork."""
    for name in ("chat_open_button", "chat_message_box", "chat_send_button"):
        assert callable(getattr(selectors_zoom, name))


def test_zoom_message_is_the_shared_exact_message() -> None:
    assert consent.CONSENT_MESSAGE == MESSAGE


def test_announcement_posts_to_zoom_chat() -> None:
    target = page("zoom_chat.html")

    assert (
        consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
        is True
    )
    assert "open the chat panel" in target.clicked
    # Humanized typing still runs; the live client then ignores the key
    # events, so the fill fallback carries the commit.
    assert target.typed_text() == MESSAGE
    assert target.filled == [MESSAGE]
    assert "send" in target.clicked


def test_ignored_trusted_send_falls_back_to_dom_click() -> None:
    """Live 2026-10-08: a trusted click on Zoom's send control is actionably
    accepted but silently ignored; the composer still holds the message and
    the DOM click posts it."""
    target = page("zoom_chat.html")

    assert consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
    assert any("el => el.click()" in expression for expression in target.evaluated)


def test_trusted_click_cannot_land_falls_back_to_dom_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live: the chat control's center renders below the fold (y=1083 in a
    1080-tall viewport), so Playwright's trusted click throws while
    ``el.click()`` opens the panel."""
    target = page("zoom_chat.html")

    def failing_click(locator: object, *, timeout_ms: int | None = None) -> None:
        raise RuntimeError("element is outside of the viewport")

    monkeypatch.setattr(consent, "click_like_human", failing_click)

    assert (
        consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
        is True
    )
    assert any("el => el.click()" in expression for expression in target.evaluated)


def test_missing_chat_control_warns_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    target = page("zoom_in_call.html")

    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert (
            consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
            is False
        )
    assert any("chat not available" in record.getMessage() for record in caplog.records)
    assert target.typed_text() == ""


def test_message_box_missing_warns(caplog: pytest.LogCaptureFixture) -> None:
    target = page("zoom_chat_no_box.html")

    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert (
            consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
            is False
        )
    assert any("chat not available" in record.getMessage() for record in caplog.records)
    # The trusted click was accepted but produced no composer; the verified
    # retry tries the DOM click once more before giving up.
    assert any("el => el.click()" in expression for expression in target.evaluated)


def test_enter_when_no_send_button() -> None:
    target = page("zoom_chat_no_send.html")

    assert (
        consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
        is True
    )
    assert target.typed_text() == MESSAGE
    assert "Enter" in target.pressed


def test_interaction_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    target = page("zoom_chat.html")
    target.mouse.move = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert (
            consent.post_chat_announcement(target, selector_set=selectors_zoom, find_timeout_s=0.0)
            is False
        )
    assert any(
        "consent chat announcement failed" in record.getMessage() for record in caplog.records
    )
