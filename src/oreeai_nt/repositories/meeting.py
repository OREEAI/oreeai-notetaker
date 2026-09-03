from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from oreeai_nt.models.meeting import Meeting
from oreeai_nt.repositories.base import BaseRepository

_LIST_COLUMNS = (
    Meeting.id,
    Meeting.title,
    Meeting.description,
    Meeting.platform,
    Meeting.status,
    Meeting.scheduled_at,
    Meeting.created_at,
    Meeting.updated_at,
)


class MeetingRepository(BaseRepository[Meeting]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Meeting, session)

    async def list(self, *, offset: int = 0, limit: int = 100) -> Sequence[Meeting]:
        stmt = (
            select(Meeting)
            .options(load_only(*_LIST_COLUMNS))
            .order_by(Meeting.created_at.desc(), Meeting.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
