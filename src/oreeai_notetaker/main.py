from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from oreeai_notetaker.api.v1.router import api_router
from oreeai_notetaker.core.cache import CacheService
from oreeai_notetaker.core.config import settings
from oreeai_notetaker.core.exceptions import AppError
from oreeai_notetaker.core.logging import setup_logging
from oreeai_notetaker.db.session import engine
from oreeai_notetaker.schemas.call import CONTRACT_VALIDATION_CODES


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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        for error in exc.errors():
            code = str(error.get("type", ""))
            if code in CONTRACT_VALIDATION_CODES:
                return JSONResponse(
                    status_code=422, content={"detail": code, "failure_reason": code}
                )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    return app


app = create_app()
