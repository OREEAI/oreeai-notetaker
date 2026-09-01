from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_nt.models.meeting import Meeting
from oreeai_nt.repositories.base import BaseRepository


class MeetingRepository(BaseRepository[Meeting]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Meeting, session)

    async def list(self, *, offset: int = 0, limit: int = 100) -> Sequence[Meeting]:
        stmt = select(Meeting).order_by(Meeting.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()
