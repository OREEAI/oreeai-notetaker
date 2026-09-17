"""Transcription provider contract (PR 7).

The runner depends on this seam, never on a concrete provider.
Concrete adapters live beside this file: ``deepgram.py`` (the settled
provider — nova-3, pre-recorded batch), ``stub.py`` (dev/staging-only
stand-in), ``assemblyai.py`` (the phase-3 seam, unimplemented).

The audio arrives as an ``AudioSource`` (from
``integrations.object_storage.base``): the storage adapter owns the
transport strategy (presigned GET URL the provider fetches vs. a local
stored-copy path the transcription adapter POSTs as bytes), so this
layer never knows where the audio lives.

Log hygiene (standing rules): adapters never log transcript text
(lengths/segment counts only), never log the ``AudioSource.local_path``,
never log the presigned URL, and never log the API key. Provider error
messages are re-raised with call-id-only text; provider response bodies
are never logged (they contain transcript text).
"""

from typing import Protocol

from pydantic import BaseModel, Field

from oreeai_notetaker.integrations.object_storage.base import AudioSource


class TranscriptionError(Exception):
    """Base class for typed transcription failures."""


class TransientTranscriptionError(TranscriptionError):
    """Retryable provider failure (429, 5xx, timeout, network).

    The retry policy lives in ``services/transcription.py``.
    """


class PermanentTranscriptionError(TranscriptionError):
    """Non-retryable provider failure (401, 400, 402, other 4xx).

    402 = insufficient credits: no retry helps; the operator must top
    up the account. Messages are call-id-safe (no transcript content,
    no audio source details).
    """


class AudioTooLarge(TranscriptionError):
    """Audio exceeds ``AUDIO_MAX_BYTES`` — refused before any request.

    Permanent by nature (the recording is the size it is); surfaced as
    ``transcription_failed:size_*`` by the runner.
    """


class SpeakerSegment(BaseModel):
    """One diarized utterance — the pinned webhook segment schema."""

    speaker: str = Field(examples=["S0", "S1"])
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    segments: list[SpeakerSegment] = Field(default_factory=list)


class TranscriptionClient(Protocol):
    """Provider seam every adapter implements.

    ``transcribe`` is synchronous-shaped (a single awaited call) even
    though some providers stream interim results: the adapter must
    resolve only on the *final* diarized result — "transcript finished"
    does not imply diarization finished.
    """

    async def transcribe(self, source: AudioSource) -> Transcript:
        """Transcribe one stored recording; diarization is non-negotiable."""
        ...
