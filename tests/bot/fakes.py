"""In-memory Playwright page double for bot state tests.

The double understands only the narrow Playwright surface used by
:mod:`bot.selectors`: role queries, text queries, one simple CSS attribute
query, visibility/count/first accessors, and non-blocking timeouts. HTML
fixtures are parsed with the standard library so no browser is needed in CI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    depth: int
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())


def _role_of(element: _Element) -> str | None:
    explicit = element.attrs.get("role", "").strip().lower()
    if explicit:
        return explicit
    if element.tag == "button":
        return "button"
    if element.tag == "textarea":
        return "textbox"
    if element.tag == "input" and element.attrs.get("type", "text").lower() in {
        "email",
        "password",
        "search",
        "tel",
        "text",
        "url",
    }:
        return "textbox"
    return None


def _accessible_name(element: _Element) -> str:
    return element.attrs.get("aria-label", "").strip() or element.text


def _is_visible(element: _Element) -> bool:
    if "hidden" in element.attrs:
        return False
    style = element.attrs.get("style", "").replace(" ", "").lower()
    return "display:none" not in style and "visibility:hidden" not in style


class _SnapshotParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag.lower(), {k.lower(): v or "" for k, v in attrs}, len(self._stack))
        self.elements.append(element)
        self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(
            _Element(tag.lower(), {k.lower(): v or "" for k, v in attrs}, len(self._stack))
        )

    def handle_endtag(self, tag: str) -> None:
        if self._stack and self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if data.strip():
            for element in self._stack:
                element.text_parts.append(data)


class FakeLocator:
    """A matching-element list with the Playwright accessors selectors use."""

    def __init__(
        self, elements: list[_Element] | tuple[_Element, ...] = (), page: FakePage | None = None
    ) -> None:
        self._elements = list(elements)
        self._page = page

    def count(self) -> int:
        return len(self._elements)

    @property
    def first(self) -> FakeLocator:
        if not self._elements:
            return FakeLocator((), self._page)
        # Prefer the most specific matching control when an ancestor also
        # contains the same accessible name or visible text.
        best = min(self._elements, key=lambda item: (len(item.text), item.depth))
        return FakeLocator((best,), self._page)

    def is_visible(self) -> bool:
        return any(_is_visible(item) for item in self._elements)

    def get_attribute(self, name: str) -> str | None:
        if not self._elements:
            return None
        return self._elements[0].attrs.get(name.lower())

    def inner_text(self) -> str:
        if not self._elements:
            return ""
        return self._elements[0].text

    def bounding_box(self) -> dict[str, float] | None:
        if not self._elements or not _is_visible(self._elements[0]):
            return None
        return {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}

    def click(self, **kwargs: object) -> None:
        del kwargs
        if self._page is not None and self._elements:
            self._page.clicked.append(_accessible_name(self._elements[0]))

    def press(self, key: str) -> None:
        if self._page is not None:
            self._page.pressed.append(key)

    def fill(self, value: str) -> None:
        if self._page is not None:
            self._page.typed.append(value)


class _FakeMouse:
    def move(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


class _FakeKeyboard:
    """Records typed characters onto the page."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    def type(self, char: str, **kwargs: object) -> None:
        del kwargs
        self._page.typed.append(char)


_SIMPLE_ATTRIBUTE_SELECTOR = re.compile(
    r"(?P<tag>[A-Za-z][\w-]*)\[(?P<attr>[\w-]+)\*=\"(?P<value>[^\"]+)\"\s*i\]"
)


def _matches_attribute(item: _Element, tag: str, attr: str, value: str) -> bool:
    return item.tag == tag and value in item.attrs.get(attr, "").casefold()


class FakePage:
    """Parse one committed fixture and answer selector queries from its DOM.

    ``clicked`` / ``pressed`` / ``typed`` record the interactions the bot
    performs (via :mod:`bot.humanize`) so tests can assert the consent
    announcement without a browser.
    """

    def __init__(self, html: str, url: str = "https://meet.google.com/abc-defg-hij") -> None:
        parser = _SnapshotParser()
        parser.feed(html)
        self._elements = parser.elements
        self.url = url
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard(self)
        self.clicked: list[str] = []
        self.pressed: list[str] = []
        self.typed: list[str] = []

    @classmethod
    def from_fixture(cls, path: Path) -> FakePage:
        return cls(path.read_text(encoding="utf-8"))

    def typed_text(self) -> str:
        """Everything typed so far (per-character typing collapses to text)."""
        return "".join(self.typed)

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None

    def screenshot(self, **kwargs: object) -> bytes:
        del kwargs
        return b""

    def evaluate(self, _expression: str) -> str:
        return ""

    def get_by_role(self, role: str, *, name: str | re.Pattern[str] | None = None) -> FakeLocator:
        wanted = role.lower()
        matches = []
        for element in self._elements:
            if _role_of(element) != wanted:
                continue
            if name is None:
                matches.append(element)
            elif isinstance(name, re.Pattern):
                if name.search(_accessible_name(element)):
                    matches.append(element)
            elif _accessible_name(element).casefold() == str(name).casefold():
                matches.append(element)
        return FakeLocator(matches, self)

    def get_by_text(self, text: str | re.Pattern[str]) -> FakeLocator:
        if isinstance(text, re.Pattern):
            return FakeLocator([item for item in self._elements if text.search(item.text)], self)
        wanted = " ".join(str(text).split()).casefold()
        return FakeLocator(
            [item for item in self._elements if wanted in item.text.casefold()], self
        )

    def locator(self, selector: str) -> FakeLocator:
        match = _SIMPLE_ATTRIBUTE_SELECTOR.fullmatch(selector.strip())
        if match is None:
            return FakeLocator((), self)
        wanted_tag = match.group("tag").lower()
        wanted_attr = match.group("attr").lower()
        wanted_value = match.group("value").casefold()
        return FakeLocator(
            [
                item
                for item in self._elements
                if _matches_attribute(item, wanted_tag, wanted_attr, wanted_value)
            ],
            self,
        )


class ScriptedPage(FakePage):
    """A page whose DOM advances one fixture per poll.

    Each `wait_for_timeout` (the loop's poll tick) loads the next fixture;
    the last fixture repeats, so open-ended loops never run out of script.
    """

    def __init__(
        self, fixtures: list[Path], url: str = "https://meet.google.com/abc-defg-hij"
    ) -> None:
        self._scripts = [path.read_text(encoding="utf-8") for path in fixtures]
        self._index = 0
        super().__init__(self._scripts[0], url=url)

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        if self._index + 1 < len(self._scripts):
            self._index += 1
            parser = _SnapshotParser()
            parser.feed(self._scripts[self._index])
            self._elements = parser.elements
        return None
