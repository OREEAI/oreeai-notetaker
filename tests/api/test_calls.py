import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.models.call import Call

CALLS_URL = "/api/v1/calls"


def call_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "user_ref": "test-user-1",
        "consent_ack": True,
        "webhook_url": "https://example.com/hooks/oreeai",
        "webhook_secret": "shhhhhhhhhhhhhhhh",
    }
    payload.update(overrides)
    return payload


async def test_create_call_returns_201(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["call_id"])
    assert body["status"] == "queued"


async def test_create_call_persists_fields(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
    call_id = created.json()["call_id"]

    response = await client.get(f"{CALLS_URL}/{call_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert body["user_ref"] == "test-user-1"
    assert body["consent_ack"] is True
    assert body["status"] == "queued"
    assert body["platform"] == "google_meet"
    assert body["failure_reason"] is None
    assert body["end_reason"] is None
    assert body["audio_url"] is None
    assert body["transcript"] is None
    assert body["webhook_url"] == "https://example.com/hooks/oreeai"
    assert body["webhook_attempts"] == 0
    assert body["bot_container_name"] is None
    assert "webhook_secret" not in body


async def test_create_call_without_consent_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        CALLS_URL, json=call_payload(consent_ack=False), headers=auth_headers
    )
    assert response.status_code == 422
    assert response.json()["failure_reason"] == "consent_missing"


async def test_create_call_without_consent_field_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = call_payload()
    del payload["consent_ack"]
    response = await client.post(CALLS_URL, json=payload, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["failure_reason"] == "consent_missing"


async def test_create_call_with_zoom_url_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        CALLS_URL,
        json=call_payload(meeting_url="https://zoom.us/j/123456789"),
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["failure_reason"] == "invalid_meeting_url"


async def test_create_call_with_lookup_url_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        CALLS_URL,
        json=call_payload(meeting_url="https://meet.google.com/lookup/abcdef"),
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["failure_reason"] == "invalid_meeting_url"


async def test_create_call_with_short_webhook_secret_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        CALLS_URL, json=call_payload(webhook_secret="too-short"), headers=auth_headers
    )
    assert response.status_code == 422


async def test_create_call_accepts_utm_params(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        CALLS_URL,
        json=call_payload(meeting_url="https://meet.google.com/abc-defg-hij?utm_source=test"),
        headers=auth_headers,
    )
    assert response.status_code == 201


async def test_create_call_returns_409_at_ceiling(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    for _ in range(settings.call_concurrency_limit):
        response = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
        assert response.status_code == 201

    response = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
    assert response.status_code == 409
    assert "CALL_CONCURRENCY_LIMIT" in response.json()["detail"]


async def test_get_call_not_found(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(f"{CALLS_URL}/{uuid.uuid4()}", headers=auth_headers)
    assert response.status_code == 404


async def test_get_call_requires_api_key(client: AsyncClient) -> None:
    response = await client.get(f"{CALLS_URL}/{uuid.uuid4()}")
    assert response.status_code == 401


async def test_post_call_requires_api_key(client: AsyncClient) -> None:
    response = await client.post(CALLS_URL, json=call_payload())
    assert response.status_code == 401


async def test_post_call_rejects_wrong_api_key(client: AsyncClient) -> None:
    response = await client.post(CALLS_URL, json=call_payload(), headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401


async def test_post_call_rejects_non_ascii_api_key(client: AsyncClient) -> None:
    response = await client.post(
        CALLS_URL, json=call_payload(), headers={"X-API-Key": b"k\xeb-unicode"}
    )
    assert response.status_code == 401


async def test_get_call_rejects_wrong_api_key(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
    call_id = created.json()["call_id"]

    response = await client.get(f"{CALLS_URL}/{call_id}", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401


async def test_list_calls_not_routed(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(CALLS_URL, headers=auth_headers)
    assert response.status_code == 405


async def test_patch_call_not_routed(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.patch(f"{CALLS_URL}/{uuid.uuid4()}", json={}, headers=auth_headers)
    assert response.status_code == 405


async def test_user_ref_is_stored_opaque(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    opaque = "crm:acct_42/user 7?keep=exact&case=Sensitive"
    created = await client.post(CALLS_URL, json=call_payload(user_ref=opaque), headers=auth_headers)
    call_id = created.json()["call_id"]

    response = await client.get(f"{CALLS_URL}/{call_id}", headers=auth_headers)
    assert response.json()["user_ref"] == opaque


async def test_created_call_is_queued_in_database(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict[str, str]
) -> None:
    created = await client.post(CALLS_URL, json=call_payload(), headers=auth_headers)
    call_id = uuid.UUID(created.json()["call_id"])

    call = await db_session.get(Call, call_id)
    assert call is not None
    assert call.status == CallStatus.queued
