from typing import Any, Protocol

from oreeai_notetaker.enums import CallPlatform


class CallPlatformClient(Protocol):
    platform: CallPlatform

    async def fetch_meeting(self, external_id: str) -> dict[str, Any]: ...

    async def fetch_transcript(self, external_id: str) -> str | None: ...
