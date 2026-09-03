from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from oreeai_nt.api.v1.router import api_router
from oreeai_nt.core.cache import CacheService
from oreeai_nt.core.config import settings
from oreeai_nt.core.exceptions import AppError
from oreeai_nt.core.logging import setup_logging
from oreeai_nt.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.log_level)
    cache = CacheService(None)
    if settings.cache_enabled:
        try:
            redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
            await redis_client.ping()
            cache = CacheService(redis_client)
        except (RedisError, OSError):
            cache = CacheService(None)
    app.state.cache = cache
    yield
    await cache.close()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.project_name, lifespan=lifespan, debug=settings.debug)
    app.state.cache = CacheService(None)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return app


app = create_app()
