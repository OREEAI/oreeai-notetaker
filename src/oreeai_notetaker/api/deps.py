import logging
import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.session import get_session
from oreeai_notetaker.repositories.call import CallRepository
from oreeai_notetaker.services.call import CallService

logger = logging.getLogger(__name__)


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


def get_call_repository(session: SessionDep) -> CallRepository:
    return CallRepository(session)


CallRepositoryDep = Annotated[CallRepository, Depends(get_call_repository)]


def get_call_service(repository: CallRepositoryDep, cache: CacheDep) -> CallService:
    return CallService(repository, cache)


CallServiceDep = Annotated[CallService, Depends(get_call_service)]


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("rejected X-API-Key from %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
