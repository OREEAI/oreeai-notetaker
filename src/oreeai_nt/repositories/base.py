import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_nt.db.base import Base


class BaseRepository[ModelType: Base]:
    def __init__(self, model: type[ModelType], session: AsyncSession) -> None:
        self.model = model
        self.session = session

    async def get(self, entity_id: uuid.UUID) -> ModelType | None:
        return await self.session.get(self.model, entity_id)

    async def list(self, *, offset: int = 0, limit: int = 100) -> Sequence[ModelType]:
        stmt = select(self.model).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, obj: ModelType) -> ModelType:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def update(self, obj: ModelType, changes: dict[str, Any]) -> ModelType:
        for field, value in changes.items():
            setattr(obj, field, value)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: ModelType) -> None:
        await self.session.delete(obj)
        await self.session.flush()
