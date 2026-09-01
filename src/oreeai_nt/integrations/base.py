from typing import Any, Protocol

from oreeai_nt.models.meeting import MeetingPlatform


class MeetingPlatformClient(Protocol):
    platform: MeetingPlatform

    async def fetch_meeting(self, external_id: str) -> dict[str, Any]: ...

    async def fetch_transcript(self, external_id: str) -> str | None: ...
