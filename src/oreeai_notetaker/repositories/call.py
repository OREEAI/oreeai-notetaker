from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.enums import ACTIVE_STATUSES
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.repositories.base import BaseRepository


class CallRepository(BaseRepository[Call]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Call, session)

    async def count_active(self) -> int:
        stmt = select(func.count()).select_from(Call).where(Call.status.in_(ACTIVE_STATUSES))
        result = await self.session.execute(stmt)
        return int(result.scalar_one())
