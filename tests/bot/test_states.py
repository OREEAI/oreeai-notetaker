"""Unit tests for pure Meet call-state predicates.

The fixtures are committed Meet DOM snapshots. They are representative, not
live captures: they encode the accessible names and visible text that the
shipped selectors match. If Meet changes its UI, update the fixture and the
corresponding selector together in `bot/selectors.py`.
"""

from pathlib import Path

import pytest

from bot import states
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures"


def page(name: str) -> FakePage:
    return FakePage.from_fixture(FIXTURES / name)


@pytest.mark.parametrize("fixture", ["waiting_room.html", "knocking.html"])
def test_waiting_room_states(fixture: str) -> None:
    waiting, waiting_detail = states.is_in_waiting_room(page(fixture))
    admitted, _ = states.is_admitted(page(fixture))
    removed, _ = states.is_removed(page(fixture))
    ended, _ = states.is_call_ended(page(fixture))
    count, _ = states.participant_count(page(fixture))
    alone, _ = states.bot_alone_in_call(page(fixture))

    assert waiting is True
    assert waiting_detail
    assert admitted is False
    assert removed is False
    assert ended is False
    assert count is None
    assert alone is False


def test_knocking_detail_names_admission_wait() -> None:
    _, detail = states.is_in_waiting_room(page("knocking.html"))

    assert "waiting for admission" in detail


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("in_call_three.html", 3),
        ("in_call_two.html", 2),
        ("in_call_alone.html", 1),
    ],
)
def test_participant_counts(fixture: str, expected: int) -> None:
    admitted, _ = states.is_admitted(page(fixture))
    count, detail = states.participant_count(page(fixture))

    assert admitted is True
    assert count == expected
    assert str(expected) in detail


def test_bare_digit_count_chip() -> None:
    # Current Meet renders the count as a bare digit on the control.
    admitted, _ = states.is_admitted(page("in_call_bare_count.html"))
    count, detail = states.participant_count(page("in_call_bare_count.html"))

    assert admitted is True
    assert count == 2
    assert "2 participants" in detail


def test_unknown_participant_count_is_not_empty() -> None:
    count, detail = states.participant_count(page("in_call_no_count.html"))
    alone, alone_detail = states.bot_alone_in_call(page("in_call_no_count.html"))

    assert count is None
    assert "unavailable" in detail
    assert alone is False
    assert "not enough evidence" in alone_detail


@pytest.mark.parametrize("fixture", ["in_call_three.html", "in_call_two.html"])
def test_bot_is_not_alone_with_other_participants(fixture: str) -> None:
    alone, detail = states.bot_alone_in_call(page(fixture))

    assert alone is False
    assert "other participants" in detail


def test_bot_alone_in_call() -> None:
    alone, detail = states.bot_alone_in_call(page("in_call_alone.html"))

    assert alone is True
    assert "only the bot remains" in detail


def test_call_ended_state() -> None:
    waiting, _ = states.is_in_waiting_room(page("call_ended.html"))
    admitted, _ = states.is_admitted(page("call_ended.html"))
    removed, _ = states.is_removed(page("call_ended.html"))
    ended, detail = states.is_call_ended(page("call_ended.html"))

    assert ended is True
    assert "meeting has ended" in detail.casefold()
    assert waiting is False
    assert admitted is False
    assert removed is False


def test_host_ended_state() -> None:
    ended, detail = states.is_call_ended(page("call_ended_host.html"))

    assert ended is True
    assert "host ended the meeting" in detail.casefold()


def test_removed_state() -> None:
    admitted, _ = states.is_admitted(page("removed.html"))
    ended, _ = states.is_call_ended(page("removed.html"))
    removed, detail = states.is_removed(page("removed.html"))

    assert removed is True
    assert "removed from the meeting" in detail.casefold()
    assert admitted is False
    assert ended is False


def test_join_blocked_page_is_none_of_the_lifecycle_states() -> None:
    waiting, _ = states.is_in_waiting_room(page("join_blocked.html"))
    admitted, _ = states.is_admitted(page("join_blocked.html"))
    removed, _ = states.is_removed(page("join_blocked.html"))
    ended, _ = states.is_call_ended(page("join_blocked.html"))

    assert waiting is False
    assert admitted is False
    assert removed is False
    assert ended is False
