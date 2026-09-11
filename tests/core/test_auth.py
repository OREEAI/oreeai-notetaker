import logging
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from oreeai_notetaker.api.deps import require_api_key
from oreeai_notetaker.core.config import settings


def make_request(client_host: str | None = "203.0.113.7") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/health",
        "headers": [],
        "client": (client_host, 12345) if client_host else None,
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


async def test_valid_key_passes() -> None:
    await require_api_key(make_request(), settings.api_key)


async def test_missing_key_raises_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(make_request(), None)
    assert exc_info.value.status_code == 401


async def test_wrong_key_raises_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(make_request(), "definitely-not-the-key")
    assert exc_info.value.status_code == 401


async def test_rejection_logs_source_ip_not_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException):
        await require_api_key(make_request("198.51.100.9"), "leaky-candidate-key")

    assert "198.51.100.9" in caplog.text
    assert "leaky-candidate-key" not in caplog.text


async def test_rejection_without_client_logs_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException):
        await require_api_key(make_request(None), "leaky-candidate-key")

    assert "unknown" in caplog.text
