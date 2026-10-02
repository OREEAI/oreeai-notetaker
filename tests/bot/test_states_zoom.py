"""Unit tests for the pure Zoom web-client state predicates (Z1 spike).

Fixtures under ``tests/bot/fixtures/zoom/`` are committed, representative
web-client DOM snapshots covering the guest join flow (trimmed to the
controls the predicates read). When the live Z1 join session corrects a
snapshot, update the fixture and ``bot/selectors_zoom.py`` together.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from bot import selectors_zoom, states_zoom
from tests.bot.fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures" / "zoom"


def page(name: str) -> FakePage:
    return FakePage.from_fixture(FIXTURES / name)


@pytest.mark.parametrize("fixture", ["zoom_prejoin.html", "zoom_prejoin_muted.html"])
def test_prejoin_states(fixture: str) -> None:
    prejoin, prejoin_detail = states_zoom.is_prejoin(page(fixture))
    admitted, _ = states_zoom.is_admitted(page(fixture))
    waiting, _ = states_zoom.is_in_waiting_room(page(fixture))
    not_started, _ = states_zoom.is_meeting_not_started(page(fixture))

    assert prejoin is True
    assert prejoin_detail
    assert admitted is False
    assert waiting is False
    assert not_started is False


def test_landing_page_offers_browser_join_and_is_not_prejoin() -> None:
    landing = page("zoom_landing.html")
    prejoin, _ = states_zoom.is_prejoin(landing)
    admitted, _ = states_zoom.is_admitted(landing)

    assert prejoin is False
    assert admitted is False
    assert selectors_zoom.browser_join_link(landing, timeout_ms=0) is not None


def test_landing_without_browser_link_has_no_wall_text() -> None:
    """The bare header "Sign In" link must not read as the CAPTCHA wall."""
    landing = page("zoom_landing_no_browser_link.html")

    assert selectors_zoom.browser_join_link(landing, timeout_ms=0) is None
    assert states_zoom.is_sign_in_required(landing)[0] is False


def test_waiting_room_with_leave_control_never_reads_as_admitted() -> None:
    waiting_page = page("zoom_waiting_room.html")
    waiting, waiting_detail = states_zoom.is_in_waiting_room(waiting_page)
    admitted, _ = states_zoom.is_admitted(waiting_page)
    prejoin, _ = states_zoom.is_prejoin(waiting_page)
    count, _ = states_zoom.participant_count(waiting_page)

    assert waiting is True
    assert waiting_detail
    assert admitted is False
    assert prejoin is False
    assert count is None


def test_meeting_not_started_is_distinct_from_waiting_room() -> None:
    not_started_page = page("zoom_meeting_not_started.html")
    not_started, not_started_detail = states_zoom.is_meeting_not_started(not_started_page)
    waiting, _ = states_zoom.is_in_waiting_room(not_started_page)
    admitted, _ = states_zoom.is_admitted(not_started_page)

    assert not_started is True
    assert not_started_detail
    assert waiting is False
    assert admitted is False


def test_in_call_states() -> None:
    in_call = page("zoom_in_call.html")
    admitted, admitted_detail = states_zoom.is_admitted(in_call)
    prejoin, _ = states_zoom.is_prejoin(in_call)
    removed, _ = states_zoom.is_removed(in_call)
    ended, _ = states_zoom.is_call_ended(in_call)

    assert admitted is True
    assert admitted_detail
    assert prejoin is False
    assert removed is False
    assert ended is False


def test_removed_and_ended_notices() -> None:
    removed_page = page("zoom_removed.html")
    removed, removed_detail = states_zoom.is_removed(removed_page)
    admitted, _ = states_zoom.is_admitted(removed_page)

    assert removed is True
    assert removed_detail
    assert admitted is False

    ended_page = page("zoom_ended.html")
    ended, ended_detail = states_zoom.is_call_ended(ended_page)

    assert ended is True
    assert ended_detail


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("zoom_in_call.html", 2),
        ("zoom_in_call_alone.html", 1),
        ("zoom_in_call_no_count.html", None),
    ],
)
def test_participant_count(fixture: str, expected: int | None) -> None:
    count, detail = states_zoom.participant_count(page(fixture))

    assert count == expected
    assert detail


def test_bot_alone_signals() -> None:
    alone_count, count_detail = states_zoom.bot_alone_in_call(page("zoom_in_call_alone.html"))
    assert alone_count is True
    assert count_detail

    others, _ = states_zoom.bot_alone_in_call(page("zoom_in_call.html"))
    assert others is False

    hint_alone, hint_detail = states_zoom.bot_alone_in_call(page("zoom_alone_hint.html"))
    assert hint_alone is True
    assert hint_detail

    unknown, _ = states_zoom.bot_alone_in_call(page("zoom_in_call_no_count.html"))
    assert unknown is False


@pytest.mark.parametrize(
    ("fixture", "predicate"),
    [
        ("zoom_sign_in_required.html", states_zoom.is_sign_in_required),
        ("zoom_only_authenticated.html", states_zoom.is_only_authenticated),
        ("zoom_desktop_app_required.html", states_zoom.is_desktop_app_required),
        ("zoom_passcode.html", states_zoom.is_passcode_screen),
    ],
)
def test_error_surfaces(fixture: str, predicate: Callable[[FakePage], tuple[bool, str]]) -> None:
    detected, detail = predicate(page(fixture))

    assert detected is True
    assert detail


def test_error_surfaces_quiet_on_normal_pages() -> None:
    for fixture in ("zoom_landing.html", "zoom_prejoin.html", "zoom_in_call.html"):
        normal = page(fixture)
        assert states_zoom.is_sign_in_required(normal)[0] is False
        assert states_zoom.is_only_authenticated(normal)[0] is False
        assert states_zoom.is_desktop_app_required(normal)[0] is False
        assert states_zoom.is_passcode_screen(normal)[0] is False
