from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_nt.core.cache import CacheService
from oreeai_nt.db.session import get_session
from oreeai_nt.repositories.meeting import MeetingRepository
from oreeai_nt.services.meeting import MeetingService


async def get_db(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncIterator[AsyncSession]:
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_cache(request: Request) -> CacheService:
    cache: CacheService = request.app.state.cache
    return cache


CacheDep = Annotated[CacheService, Depends(get_cache)]


def get_meeting_repository(session: SessionDep) -> MeetingRepository:
    return MeetingRepository(session)


MeetingRepositoryDep = Annotated[MeetingRepository, Depends(get_meeting_repository)]


def get_meeting_service(
    repository: MeetingRepositoryDep,
    cache: CacheDep,
) -> MeetingService:
    return MeetingService(repository, cache)


MeetingServiceDep = Annotated[MeetingService, Depends(get_meeting_service)]
