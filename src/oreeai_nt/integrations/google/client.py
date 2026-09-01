from typing import Any

from oreeai_nt.models.meeting import MeetingPlatform


class GoogleMeetClient:
    platform = MeetingPlatform.google_meet

    async def fetch_meeting(self, external_id: str) -> dict[str, Any]:
        raise NotImplementedError("Google Meet integration is not implemented yet")

    async def fetch_transcript(self, external_id: str) -> str | None:
        raise NotImplementedError("Google Meet integration is not implemented yet")
