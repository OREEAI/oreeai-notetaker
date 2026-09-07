"""Unit tests for authenticated-mode configuration and session detection."""

from pathlib import Path

import pytest
from bot.join_meet import (
    AUTH_MODE_ANONYMOUS,
    AUTH_MODE_AUTHENTICATED,
    parse_auth_mode,
    validate_profile_dir,
)

from bot import states
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures"


def page(name: str) -> FakePage:
    return FakePage.from_fixture(FIXTURES / name)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", AUTH_MODE_ANONYMOUS),
        ("anonymous", AUTH_MODE_ANONYMOUS),
        ("  ANONYMOUS  ", AUTH_MODE_ANONYMOUS),
        ("authenticated", AUTH_MODE_AUTHENTICATED),
        ("  Authenticated  ", AUTH_MODE_AUTHENTICATED),
        ("bogus", AUTH_MODE_ANONYMOUS),
    ],
)
def test_parse_auth_mode(raw: str, expected: str) -> None:
    assert parse_auth_mode(raw) == expected


def test_validate_profile_dir(tmp_path: Path) -> None:
    assert validate_profile_dir("") is not None
    assert validate_profile_dir(str(tmp_path / "missing")) is not None
    assert validate_profile_dir(str(tmp_path)) is None


def test_signed_in_state() -> None:
    signed_in, detail = states.is_signed_in(page("signed_in.html"))

    assert signed_in is True
    assert detail


def test_signed_out_state() -> None:
    signed_in, _ = states.is_signed_in(page("waiting_room.html"))

    assert signed_in is False


def test_first_run_promo_text_is_not_a_session() -> None:
    # Regression: Chrome's first-run promo copy contains bare "Google
    # Account" text with no session behind it (caught by the login smoke).
    signed_in, _ = states.is_signed_in(page("signed_out_promo.html"))

    assert signed_in is False
