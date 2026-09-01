from fastapi import APIRouter

from oreeai_nt.api.v1 import health, meetings

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(meetings.router, prefix="/meetings", tags=["meetings"])
