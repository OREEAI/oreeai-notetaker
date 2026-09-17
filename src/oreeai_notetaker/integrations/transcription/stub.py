"""Stub transcription provider — dev/staging stand-in, never production.

Mirrors the PR 6 local-storage fallback: lets the full runner flow
(exit → upload → transcribe → ``done`` → webhook → retention) exercise
without any provider account or credentials.

Honesty is the contract: this stub returns an **empty transcript** — an
empty transcript is a chunk-specified legitimate ``done`` state (a call
where nobody spoke), so shipping it into ``done`` is honest about what
the stub can hear: nothing. It never fabricates words into webhook
consumers' hands, and it never reads the audio at all (no network, no
file reads — ``source`` is ignored).

Production refuses to run with it: ``build_transcription_service``
raises ``ConfigurationError`` when ``ENVIRONMENT=production`` selects
``stub`` (fail fast at runner startup, like the storage fail-fast).
"""

import logging

from oreeai_notetaker.integrations.object_storage.base import AudioSource
from oreeai_notetaker.integrations.transcription.base import Transcript

logger = logging.getLogger(__name__)


class StubTranscriptionClient:
    """``TranscriptionClient`` stand-in: honest empty transcript, no I/O.

    ``supports_realtime`` is deliberately ``False`` — the stub stands in
    for the batch path only; the realtime seam stays with the real
    provider (Deepgram) for phase 3.
    """

    supports_realtime: bool = False
    """No realtime here; phase 3 goes through the real provider."""

    def __init__(self, *, log_warnings: bool = True) -> None:
        self._log_warnings = log_warnings

    def warn_once(self) -> None:
        """Loud startup warning (mirrors the local storage fallback)."""
        if self._log_warnings:
            logger.warning(
                "TRANSCRIPTION_PROVIDER=stub: every call will land done with "
                "an EMPTY transcript (segments=[]) — dev/staging only, never "
                "production"
            )

    async def transcribe(self, source: AudioSource) -> Transcript:
        """Honest empty transcript: no fabricated words, no audio I/O.

        The empty-transcript ``done`` state is legitimate per the chunk
        (e.g. everyone muted); the stub's only observable difference
        from a real provider is that it is always empty.
        """
        del source  # never read: the stub performs no I/O on the audio
        return Transcript(segments=[])
