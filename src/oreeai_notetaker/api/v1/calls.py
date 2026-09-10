import uuid

from fastapi import APIRouter

from oreeai_notetaker.api.deps import CallServiceDep
from oreeai_notetaker.schemas.call import CallCreate, CallCreated, CallRead

router = APIRouter()


@router.post("", response_model=CallCreated, status_code=201)
async def create_call(data: CallCreate, service: CallServiceDep) -> CallCreated:
    call = await service.create(data)
    return CallCreated(call_id=call.id, status=call.status)


@router.get("/{call_id}", response_model=CallRead)
async def get_call(call_id: uuid.UUID, service: CallServiceDep) -> CallRead:
    return await service.get(call_id)
