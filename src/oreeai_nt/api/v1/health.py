from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from oreeai_nt.api.deps import CacheDep, SessionDep

router = APIRouter()


@router.get("/health")
async def health(session: SessionDep, cache: CacheDep) -> JSONResponse:
    database = "up"
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        await session.rollback()
        database = "down"

    cache_status = "up" if await cache.ping() else "down"
    healthy = database == "up"
    payload: dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "components": {"database": database, "cache": cache_status},
    }
    return JSONResponse(status_code=200 if healthy else 503, content=payload)
