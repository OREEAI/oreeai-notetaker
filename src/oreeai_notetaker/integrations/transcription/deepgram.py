"""Deepgram pre-recorded (batch) transcription — the settled provider.

Provider decision (CTO, 2026-09-15): Deepgram ``nova-3``, pre-recorded
batch. Rationale lives in ``docs/transcription.md``; short version: both
candidates meet the batch+realtime-on-one-account constraint, but
Deepgram includes speaker diarization free on batch and streaming and
bills audio minutes, while AssemblyAI charges separately for
diarization and bills streaming per WebSocket session including idle
time — our bots deliberately dwell in waiting rooms up to 10 min and
empty rooms up to 5 min, which phase 3 would have paid for.

Request shape (both transports, pinned and asserted in tests):

- ``POST https://api.deepgram.com/v1/listen``
- ``Authorization: Token <DEEPGRAM_API_KEY>`` header — the key is never
  logged.
- Query: ``model=nova-3``, ``diarize_model=latest``, ``utterances=true``,
  ``punctuate=true``, ``smart_format=true``. Diarization is enabled via
  ``diarize_model=latest`` per
  https://developers.deepgram.com/docs/diarization/ — the legacy
  ``diarize=true`` is deprecated and requests that set *both*
  ``diarize`` and ``diarize_model`` are rejected by the API, so exactly
  one parameter is pinned here.
- Transport, decided by the ``AudioSource`` the storage adapter built
  (the strategy lives in the storage adapter, not here):
  - ``source.url`` → JSON body ``{"url": <presigned GET>}`` — the
    provider fetches from the bucket; the presigned URL is time-limited
    and never logged.
  - ``source.local_path`` → raw binary body (``Content-Type:
    audio/wav``) streamed from the stored copy — a remote provider
    cannot fetch a ``file://`` location; the path itself is never
    logged and never leaves the host except as request bytes.

Provider behavior pins (do not regress):

- **Deepgram does not store transcripts — the synchronous response is
  the only copy** (their docs' own wording). A misparsed 200 therefore
  means the transcript is lost: parse failures are permanent errors,
  never silent empty results.
- The pre-recorded endpoint is synchronous — the returned response IS
  the final result including diarization ("transcript finished" ≠
  "diarization finished" is a streaming-era race that batch does not
  have; this adapter must never move to a callback/202 flow, which
  would re-introduce one — a 202 is treated as an error, not success).
- The WAV arrives as PR 1 pinned it (16 kHz mono s16le PCM); Deepgram
  accepts it directly and nothing here transcodes (PR 1 audio
  contract, never changes).

Error mapping (permanent vs. transient is the retry contract in
``services/transcription.py``): 429 and 5xx and timeouts/transport
failures are transient; 401/400/402 and any other 4xx are permanent —
402 is insufficient credits and needs operator action, not retries.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
)
from oreeai_notetaker.integrations.transcription.base import (
    PermanentTranscriptionError,
    SpeakerSegment,
    Transcript,
    TransientTranscriptionError,
)

logger = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"

MODEL = "nova-3"
DIARIZE_MODEL = "latest"
PARAMS: dict[str, str] = {
    "model": MODEL,
    "diarize_model": DIARIZE_MODEL,
    "utterances": "true",
    "punctuate": "true",
    "smart_format": "true",
}

# Deepgram 504s pre-recorded requests whose processing exceeds 10 min
# (their docs' Limits) — the client must out-wait that so we receive the
# typed 504 (transient, retried) instead of timing out ourselves.
READ_TIMEOUT_S = 660.0
CONNECT_TIMEOUT_S = 10.0
_FILE_CHUNK_BYTES = 1024 * 1024

_STATUS_HINTS: dict[int, str] = {
    401: "auth rejected — check DEEPGRAM_API_KEY",
    400: "request rejected — bad parameters or audio",
    402: "insufficient credits — operator action required",
}


class DeepgramTranscriptionClient:
    """``TranscriptionClient`` over Deepgram's pre-recorded batch API.

    ``transport`` exists for tests (``httpx.MockTransport``), mirroring
    ``workers.webhook_dispatcher``.
    """

    supports_realtime: bool = True
    """Realtime (streaming) is available on the *same* account/key.

    Reserved for the phase 3 live trainer; PR 7 builds only the batch
    path. Docs: https://developers.deepgram.com/docs/live-streaming-audio/
    """

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        listen_url: str = DEEPGRAM_LISTEN_URL,
    ) -> None:
        self._api_key = api_key
        self._listen_url = listen_url
        self._transport = transport

    @classmethod
    def from_settings(cls) -> "DeepgramTranscriptionClient":
        """Build from settings; ``ConfigurationError`` when the key is
        unset (fail fast at runner startup, like the storage adapter)."""
        if not settings.deepgram_api_key:
            raise ConfigurationError(
                "DEEPGRAM_API_KEY is not configured (TRANSCRIPTION_PROVIDER=deepgram)"
            )
        return cls(settings.deepgram_api_key)

    async def transcribe(self, source: AudioSource) -> Transcript:
        """Transcribe one stored recording; speaker labels render as the
        provider's ints per the webhook contract: ``S0``, ``S1``."""
        timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        auth = {"Authorization": f"Token {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                if source.url is not None:
                    response = await client.post(
                        self._listen_url,
                        params=PARAMS,
                        json={"url": source.url},
                        headers=auth,
                    )
                elif source.local_path is not None:
                    response = await client.post(
                        self._listen_url,
                        params=PARAMS,
                        headers={**auth, "Content-Type": "audio/wav"},
                        content=_file_chunks(source.local_path),
                    )
                else:  # unreachable: AudioSource validates exactly-one-source
                    raise PermanentTranscriptionError("audio source has no transport")
        except PermanentTranscriptionError:
            raise
        except httpx.TimeoutException as exc:
            raise TransientTranscriptionError("deepgram request timed out") from exc
        except httpx.TransportError as exc:
            raise TransientTranscriptionError("deepgram connection failed") from exc
        except OSError as exc:
            # The binary-body stream reads the stored copy from disk; a
            # mid-stream failure means the stored object is gone/broken —
            # retrying the request cannot recreate it.
            raise PermanentTranscriptionError("stored audio unreadable") from exc

        status = response.status_code
        if status == 429 or status >= 500:
            raise TransientTranscriptionError(f"deepgram returned HTTP {status}")
        if status >= 300:
            hint = _STATUS_HINTS.get(status)
            detail = f"deepgram returned HTTP {status}"
            if hint:
                detail = f"{detail} ({hint})"
            raise PermanentTranscriptionError(detail)

        try:
            payload = response.json()
        except ValueError as exc:
            # Pinned: the response is the only copy of the transcript —
            # an unusable body is a lost transcript, not a retryable one.
            raise PermanentTranscriptionError("deepgram returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise PermanentTranscriptionError("deepgram response shape unexpected")
        return parse_response(payload)


def parse_response(payload: dict[str, Any]) -> Transcript:
    """Map a Deepgram pre-recorded response into a ``Transcript``.

    Primary source: ``results.utterances[]`` (the ``utterances=true``
    param), each carrying ``transcript``/``start``/``end``/``speaker`` —
    a 1:1 map onto the pinned segment schema. Fallback (param ignored by
    the provider): group ``alternatives[].words[]`` into same-speaker
    runs. Never raise with transcript content in the message.

    Honesty pins: an empty ``utterances`` list is the honest silent-call
    result and maps to ``Transcript(segments=[])``. But a NON-empty
    utterances list that yields zero valid segments is provider drift
    (e.g. ``speaker`` turned null after an API change) — the response is
    the only copy of the transcript, so a systematic drop must fail the
    call permanently, not masquerade as "nobody spoke".
    """
    results = payload.get("results")
    if not isinstance(results, dict):
        raise PermanentTranscriptionError("deepgram response missing results object")
    utterances = results.get("utterances")
    if isinstance(utterances, list):
        segments, dropped = _segments_from_utterances(utterances)
        if dropped:
            logger.warning("deepgram utterances: dropped=%s unparseable entries", dropped)
        if not segments and utterances:
            raise PermanentTranscriptionError(
                "deepgram response has utterances but none parsed as segments"
            )
        return Transcript(segments=segments)
    alternatives = [
        alt
        for channel in _as_dicts(results.get("channels"))
        for alt in _as_dicts(channel.get("alternatives"))
    ]
    return Transcript(segments=_segments_from_words(alternatives))


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _segments_from_utterances(utterances: list[Any]) -> tuple[list[SpeakerSegment], int]:
    segments: list[SpeakerSegment] = []
    dropped = 0
    for utterance in utterances:
        if not isinstance(utterance, dict):
            dropped += 1
            continue
        speaker = utterance.get("speaker")
        start = utterance.get("start")
        end = utterance.get("end")
        text = utterance.get("transcript")
        if (
            not isinstance(speaker, int)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not isinstance(text, str)
        ):
            dropped += 1
            continue
        segments.append(
            SpeakerSegment(speaker=f"S{speaker}", start=float(start), end=float(end), text=text)
        )
    return segments, dropped


def _segments_from_words(alternatives: list[dict[str, Any]]) -> list[SpeakerSegment]:
    """Fallback segmentation: consecutive same-speaker words → segments."""
    segments: list[SpeakerSegment] = []
    for alt in alternatives:
        for word in _as_dicts(alt.get("words")):
            speaker = word.get("speaker")
            start = word.get("start")
            end = word.get("end")
            if (
                not isinstance(speaker, int)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
            ):
                continue
            text = str(word.get("punctuated_word") or word.get("word") or "")
            if segments and segments[-1].speaker == f"S{speaker}":
                segments[-1].end = float(end)
                segments[-1].text = f"{segments[-1].text} {text}".strip()
            else:
                segments.append(
                    SpeakerSegment(
                        speaker=f"S{speaker}",
                        start=float(start),
                        end=float(end),
                        text=text,
                    )
                )
    return segments


async def _file_chunks(path: Path, chunk_size: int = _FILE_CHUNK_BYTES) -> AsyncIterator[bytes]:
    """Stream the stored WAV in chunks without loading it into RAM.

    Every read hops to a thread (``asyncio.to_thread``) so even a 2 GB
    file never blocks the runner's event loop.
    """
    file_handle = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(file_handle.read, chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(file_handle.close)
