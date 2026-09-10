from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from oreeai_notetaker.api.deps import get_db


async def _seed_heartbeat(app: FastAPI) -> None:
    key = app.state.cache.key("runner", "heartbeat")
    await app.state.cache.set_json(key, "2026-09-10T00:00:00Z")


async def test_health_requires_api_key(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 401


async def test_health_rejects_wrong_key(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401


async def test_health_returns_ok(
    app: FastAPI, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _seed_heartbeat(app)

    response = await client.get("/api/v1/health", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["database"] == "up"
    assert body["components"]["runner"] == "up"


async def test_health_returns_503_when_runner_down(
    app: FastAPI, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/health", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["database"] == "up"
    assert body["components"]["runner"] == "down"


async def test_health_returns_503_when_database_down(
    app: FastAPI, client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _seed_heartbeat(app)

    class BrokenSession:
        async def execute(self, *args: object, **kwargs: object) -> object:
            raise SQLAlchemyError("database unavailable")

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def broken_get_db() -> Any:
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_get_db

    response = await client.get("/api/v1/health", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["database"] == "down"
    assert body["components"]["runner"] == "up"
