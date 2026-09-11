# ruff: noqa: E402
import os

if not os.environ.get("API_KEY"):
    os.environ["API_KEY"] = "test-api-key"

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from oreeai_notetaker.api.deps import get_db
from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.base import Base
from oreeai_notetaker.main import create_app


@pytest.fixture
async def db_engine() -> AsyncIterator[Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: Any) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> Any:
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> Any:
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_cache() -> CacheService:
    return CacheService(FakeRedis())  # type: ignore[arg-type]


@pytest.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    application = create_app()
    application.state.cache = CacheService(FakeRedis())  # type: ignore[arg-type]

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    application.dependency_overrides[get_db] = override_get_db
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def api_key() -> str:
    return settings.api_key


@pytest.fixture
def auth_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}
