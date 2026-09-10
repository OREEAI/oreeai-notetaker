from typing import Any


class ZoomClient:
    platform: str = "zoom"

    async def fetch_meeting(self, external_id: str) -> dict[str, Any]:
        raise NotImplementedError("Zoom integration is not implemented yet")

    async def fetch_transcript(self, external_id: str) -> str | None:
        raise NotImplementedError("Zoom integration is not implemented yet")
