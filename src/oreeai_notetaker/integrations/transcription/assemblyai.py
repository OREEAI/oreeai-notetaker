"""AssemblyAI transcription — phase 3 seam, deliberately unimplemented.

Provider settled for PR 7 is Deepgram (nova-3 batch); this file keeps
the ``TranscriptionClient`` seam in place so the phase 3 realtime/live-
trainer work can slot in behind the same service without touching the
runner. Any call raises ``NotImplementedError`` — that is the seam
signal, not an error state the runner can hit (the service factory
never selects ``assemblyai`` today; ``TRANSCRIPTION_PROVIDER`` only
accepts ``deepgram`` | ``stub``).
"""

from oreeai_notetaker.integrations.object_storage.base import AudioSource
from oreeai_notetaker.integrations.transcription.base import Transcript


class AssemblyAITranscriptionClient:
    """``TranscriptionClient`` seam for AssemblyAI — unimplemented by
    design until the phase 3 realtime work picks a realtime provider."""

    supports_realtime: bool = True
    """AssemblyAI streams realtime; irrelevant until the seam activates."""

    def __init__(self) -> None:
        self._api_key_placeholder = None

    async def transcribe(self, source: AudioSource) -> Transcript:
        _ = source
        raise NotImplementedError(
            "AssemblyAI is not built (PR 7 settled on Deepgram); phase 3 seam only"
        )
