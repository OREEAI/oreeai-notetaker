"""Transcription service — provider selection, size guard, retry (PR 7).

The runner calls ``transcribe(source)`` with the ``AudioSource`` the
storage adapter built; this service owns the chunk's policy:

- **Size guard**: refuse before any provider call when
  ``source.size_bytes`` exceeds ``AUDIO_MAX_BYTES`` (typed
  ``AudioTooLarge``, permanent — surfaced as
  ``transcription_failed:size_*`` by the runner).
- **Retry policy**: transient errors (429/5xx/timeout/transport) get 3
  attempts with 1/4/16 s backoff; permanent errors (401/400/402, other
  4xx) fail immediately. Exhausted transient attempts re-raise as
  permanent so the runner's failure path is uniform.

Provider selection (``build_transcription_service``, cached per process
like the storage builder; fail fast at runner startup):

- ``deepgram`` → the real adapter (``DEEPGRAM_API_KEY`` required).
- ``stub`` → dev/staging-only stand-in, honest empty transcript, loud
  warning; production raises ``ConfigurationError``.
- unset → production raises ``ConfigurationError``; dev/staging falls
  back to stub with a loud warning. Unknown values cannot reach this
  factory: ``TRANSCRIPTION_PROVIDER`` is validated in settings
  (``deepgram`` | ``stub``), so a typo fail-fasts in every environment.

Log hygiene: this layer logs only byte/segment/char COUNTS — never
transcript text, never the audio source path, never the presigned URL,
never the API key.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
)
from oreeai_notetaker.integrations.transcription.base import (
    AudioTooLarge,
    PermanentTranscriptionError,
    Transcript,
    TranscriptionClient,
    TransientTranscriptionError,
)
from oreeai_notetaker.integrations.transcription.deepgram import (
    DeepgramTranscriptionClient,
)
from oreeai_notetaker.integrations.transcription.stub import StubTranscriptionClient

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]

RETRY_ATTEMPTS = 3
BACKOFF_S: tuple[float, ...] = (1.0, 4.0, 16.0)

# PR 1 audio contract: 16 kHz mono 16-bit PCM = 32,000 bytes/s — used only
# for the estimated-duration log line (counts, never bytes or paths).
_ESTIMATED_BYTES_PER_SECOND = 32_000


class TranscriptionService:
    """Facade over a ``TranscriptionClient`` — what the runner calls."""

    def __init__(
        self,
        client: TranscriptionClient,
        *,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._client = client
        self._sleeper = sleeper or _default_sleep

    async def transcribe(self, source: AudioSource) -> Transcript:
        """Transcribe with the size guard and transient retry policy."""
        if source.size_bytes > settings.audio_max_bytes:
            raise AudioTooLarge(
                f"audio too large: {source.size_bytes} bytes > "
                f"AUDIO_MAX_BYTES ({settings.audio_max_bytes})"
            )
        estimated_seconds = source.size_bytes // _ESTIMATED_BYTES_PER_SECOND
        logger.info(
            "transcribing audio: %s bytes (~%s s estimated at 16 kHz mono)",
            source.size_bytes,
            estimated_seconds,
        )
        return await self._transcribe_with_retries(source)

    async def _transcribe_with_retries(self, source: AudioSource) -> Transcript:
        last_error: TransientTranscriptionError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                transcript = await self._client.transcribe(source)
            except TransientTranscriptionError as exc:
                last_error = exc
                if attempt < RETRY_ATTEMPTS - 1:
                    delay = BACKOFF_S[attempt]
                    logger.warning(
                        "transcription attempt %s/%s failed (transient: %s); retrying in %s s",
                        attempt + 1,
                        RETRY_ATTEMPTS,
                        exc,
                        delay,
                    )
                    await self._sleeper(delay)
                continue
            logger.info(
                "transcript ready: segments=%s chars=%s",
                len(transcript.segments),
                sum(len(segment.text) for segment in transcript.segments),
            )
            return transcript
        raise PermanentTranscriptionError(
            f"transcription failed after {RETRY_ATTEMPTS} attempts: {last_error}"
        ) from last_error


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


_transcription_service: TranscriptionService | None = None


def build_transcription_service() -> TranscriptionService:
    """Select and build the transcription service for this process.

    Built once at runner startup so a broken transcription configuration
    fails fast (``ConfigurationError``) instead of at first call.
    """
    global _transcription_service
    if _transcription_service is not None:
        return _transcription_service
    provider = settings.transcription_provider
    if provider == "deepgram":
        service = TranscriptionService(DeepgramTranscriptionClient.from_settings())
    elif provider == "stub":
        if settings.environment == "production":
            raise ConfigurationError(
                "TRANSCRIPTION_PROVIDER=stub is not valid in production: "
                "configure the real provider"
            )
        client = StubTranscriptionClient()
        client.warn_once()
        service = TranscriptionService(client)
    elif provider is None:
        if settings.environment == "production":
            raise ConfigurationError(
                "TRANSCRIPTION_PROVIDER is not configured: production requires "
                "a transcription provider (deepgram)"
            )
        client = StubTranscriptionClient(log_warnings=False)
        logger.warning(
            "TRANSCRIPTION_PROVIDER unset; falling back to the stub provider "
            "(every call lands done with an EMPTY transcript) — dev/staging only"
        )
        service = TranscriptionService(client)
    else:  # unreachable: settings validate the Literal
        raise ConfigurationError(f"unknown TRANSCRIPTION_PROVIDER value {provider!r}")
    _transcription_service = service
    return service


def reset_transcription_service() -> None:
    """Drop the cached service (test seam; the runner never resets)."""
    global _transcription_service
    _transcription_service = None
