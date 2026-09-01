from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oreeai_nt.core.config import settings

engine_kwargs: dict[str, Any] = {"echo": settings.db_echo, "pool_pre_ping": True}
if settings.database_url.startswith("postgresql"):
    engine_kwargs["pool_size"] = settings.db_pool_size
    engine_kwargs["max_overflow"] = settings.db_max_overflow

engine = create_async_engine(settings.database_url, **engine_kwargs)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
