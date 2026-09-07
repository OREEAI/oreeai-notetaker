"""Background job hooks for meeting bots, transcription and summarization.

Intentionally process-local for now. When job volume warrants it, swap these
call sites to a real queue (Celery or ARQ) without touching the service layer.
"""

import uuid


async def process_meeting_recording(meeting_id: uuid.UUID) -> None:
    raise NotImplementedError("Meeting recording processing is not implemented yet")


async def generate_meeting_notes(meeting_id: uuid.UUID) -> None:
    raise NotImplementedError("AI note generation is not implemented yet")
