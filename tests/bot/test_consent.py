"""Unit tests for the PR 3 consent layer: fixed name, ack gate, identity, chat."""

import logging
from pathlib import Path

import pytest
from bot.listeners import EXIT_CONSENT_MISSING

from bot import consent
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures"

MESSAGE = (
    "Hi, this is Oree Notetaker. This call is being recorded and transcribed "
    "for note-taking. Let me know if you'd like me to leave."
)


def page(name: str) -> FakePage:
    return FakePage.from_fixture(FIXTURES / name)


def test_message_is_exact() -> None:
    assert consent.CONSENT_MESSAGE == MESSAGE
    assert consent.BOT_NAME_FIXED == "Oree Notetaker"


def test_resolve_bot_name_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("BOT_NAME", raising=False)
    with caplog.at_level(logging.WARNING):
        assert consent.resolve_bot_name() == "Oree Notetaker"
    assert not caplog.records


def test_resolve_bot_name_exact_ok(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BOT_NAME", "Oree Notetaker")
    with caplog.at_level(logging.WARNING):
        assert consent.resolve_bot_name() == "Oree Notetaker"
    assert not caplog.records


def test_resolve_bot_name_deprecation_warns_both_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BOT_NAME", "Alex")
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.resolve_bot_name() == "Oree Notetaker"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "Alex" in message
    assert "Oree Notetaker" in message
    assert "deprecated" in message.lower()


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "  true  "])
def test_consent_granted(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CONSENT_ACK", raw)
    assert consent.consent_granted() is True


@pytest.mark.parametrize("raw", ["", "false", "no", "0", "maybe"])
def test_consent_not_granted(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CONSENT_ACK", raw)
    assert consent.consent_granted() is False


def test_consent_exit_code_matches_contracts() -> None:
    assert EXIT_CONSENT_MISSING == 6


def test_environment_default_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert consent.environment() == "local"
    monkeypatch.setenv("ENVIRONMENT", "Staging")
    assert consent.environment() == "staging"


def test_account_display_name_parsed() -> None:
    assert consent.account_display_name(page("signed_in.html")) == "Oree Notetaker"


def test_account_display_name_absent_is_none() -> None:
    assert consent.account_display_name(page("waiting_room.html")) is None


def test_verify_identity_match_is_silent() -> None:
    assert consent.verify_consent_identity("Oree Notetaker", environment="production") is None


def test_verify_identity_other_warns_in_development(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.verify_consent_identity("Jay", environment="local") is None
    assert any("Jay" in record.getMessage() for record in caplog.records)


def test_verify_identity_other_fatal_in_production() -> None:
    reason = consent.verify_consent_identity("Jay", environment="production")
    assert reason is not None
    assert "Jay" in reason
    assert "Oree Notetaker" in reason


def test_verify_identity_unknown_warns_in_development(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.verify_consent_identity(None, environment="staging") is None
    assert any("could not determine" in record.getMessage() for record in caplog.records)


def test_verify_identity_unknown_fatal_in_production() -> None:
    reason = consent.verify_consent_identity(None, environment="production")
    assert reason is not None
    assert "could not determine" in reason


def test_post_chat_announcement_success() -> None:
    target = page("in_call_chat.html")
    assert consent.post_chat_announcement(target, find_timeout_s=0.0) is True
    assert "Chat with everyone (n)" in target.clicked
    assert target.typed_text() == MESSAGE
    assert "Send message" in target.clicked


def test_post_chat_announcement_no_chat_warns(caplog: pytest.LogCaptureFixture) -> None:
    target = page("in_call_three.html")
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.post_chat_announcement(target, find_timeout_s=0.0) is False
    assert any("chat not available" in record.getMessage() for record in caplog.records)
    assert target.typed_text() == ""


def test_post_chat_announcement_message_box_missing_warns(caplog: pytest.LogCaptureFixture) -> None:
    target = page("in_call_no_chat_box.html")
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.post_chat_announcement(target, find_timeout_s=0.0) is False
    assert any("chat not available" in record.getMessage() for record in caplog.records)


def test_post_chat_announcement_error_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    target = page("in_call_chat.html")
    target.mouse.move = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="oreeai.bot.consent"):
        assert consent.post_chat_announcement(target, find_timeout_s=0.0) is False
    assert any(
        "consent chat announcement failed" in record.getMessage() for record in caplog.records
    )


def test_post_chat_announcement_enter_when_no_send_button() -> None:
    target = page("in_call_chat_no_send.html")
    assert consent.post_chat_announcement(target, find_timeout_s=0.0) is True
    assert target.typed_text() == MESSAGE
    assert "Enter" in target.pressed
