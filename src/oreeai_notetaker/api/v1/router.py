from fastapi import APIRouter, Depends

from oreeai_notetaker.api.deps import require_api_key
from oreeai_notetaker.api.v1 import calls, health

api_router = APIRouter(dependencies=[Depends(require_api_key)])
api_router.include_router(health.router, tags=["health"])
api_router.include_router(calls.router, prefix="/calls", tags=["calls"])
