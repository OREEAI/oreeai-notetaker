"""Transcription provider adapters (PR 7).

- ``deepgram.py`` — the settled provider (nova-3, pre-recorded batch,
  diarization via ``diarize_model=latest``).
- ``stub.py`` — dev/staging-only stand-in (honest empty transcript).
- ``assemblyai.py`` — unimplemented phase 3 seam.
"""

from oreeai_notetaker.integrations.transcription.base import (
    AudioTooLarge,
    PermanentTranscriptionError,
    SpeakerSegment,
    Transcript,
    TranscriptionClient,
    TranscriptionError,
    TransientTranscriptionError,
)

__all__ = [
    "AudioTooLarge",
    "PermanentTranscriptionError",
    "SpeakerSegment",
    "Transcript",
    "TranscriptionClient",
    "TranscriptionError",
    "TransientTranscriptionError",
]
