from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oreeai_nt.api.deps import get_db
from oreeai_nt.core.cache import CacheService
from oreeai_nt.db.base import Base
from oreeai_nt.main import create_app


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    application = create_app()

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
