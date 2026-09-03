from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from oreeai_nt.api.deps import get_db


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["database"] == "up"


async def test_health_returns_503_when_database_down(app: FastAPI, client: AsyncClient) -> None:
    class BrokenSession:
        async def execute(self, *args: object, **kwargs: object) -> object:
            raise SQLAlchemyError("database unavailable")

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def broken_get_db() -> AsyncIterator[BrokenSession]:
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_get_db

    response = await client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["database"] == "down"
