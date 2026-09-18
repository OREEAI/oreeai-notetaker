"""Object-storage adapter contract for call audio (PR 6).

The runner (after a clean bot exit) and the retention worker depend on
this seam, not on any concrete provider. Concrete adapters live beside
this file (``s3.py`` for every S3-API-compatible provider; ``local.py``
as the dev/staging stand-in) and are built from settings via
``services/storage.build_object_storage_service``.

Log hygiene (standing rules): adapters never log audio bytes, never log
the object key, and never see ``user_ref`` at all — a storage log line
therefore cannot correlate a recording path or object key with a
``user_ref``.
"""

import uuid
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, model_validator


class ObjectStorageError(Exception):
    """Base class for typed object-storage failures."""


class ConfigurationError(ObjectStorageError):
    """Raised when the storage configuration is missing or invalid.

    Surface at startup (runner build time), not at first upload — a
    runner that cannot store audio must refuse to start rather than
    silently poison every call.
    """


class UploadFailed(ObjectStorageError):
    """Raised when an audio upload did not land.

    The message carries only the call id — the underlying provider
    exception is chained (``from``) but its text (which embeds the
    object key) is never logged by this layer.
    """


class DeleteFailed(ObjectStorageError):
    """Raised when an audio deletion failed transiently.

    Deleting a missing object is a no-op success (S3 semantics), so a
    missing object is never a failure; the retention sweep relies on
    that.
    """


class SourceUnavailable(ObjectStorageError):
    """Raised when the stored audio cannot be made transcribable.

    Raised by ``transcribable_source`` when the stored object is gone
    (HEAD 404, missing local copy) or unreadable — the upload already
    succeeded, so this is an operational anomaly, not an upload failure.
    Raised with a call-id-only message: provider exceptions (which embed
    the object key) are chained but never carried in the text.
    """


class AudioSource(BaseModel):
    """Where the transcription provider gets the audio from (PR 7).

    The transport strategy is owned by the storage adapter, not by the
    caller: the Deepgram pre-recorded API accepts both a fetchable URL
    (provider fetches; presigned, time-limited) and a raw binary body
    (we upload the bytes ourselves; used by the dev-local adapter whose
    ``file://`` location is not fetchable by a remote provider).

    Exactly one of ``url`` / ``local_path`` is set:

    - ``url`` — presigned, time-limited GET link (presigned-only
      serving rule intact; never a public link). Built by the S3
      adapter from ``S3_PRESIGN_TTL_SECONDS``.
    - ``local_path`` — the adapter's own stored copy on this host.
      Dev-only; never leaves the host except as request bytes handed
      to the provider. Callers must never log it (standing rule).
    """

    url: str | None = None
    local_path: Path | None = None
    size_bytes: int

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "AudioSource":
        if (self.url is None) == (self.local_path is None):
            raise ValueError("AudioSource requires exactly one of url or local_path")
        return self


def audio_key(call_id: uuid.UUID) -> str:
    """Canonical object key layout — pinned: ``calls/<call_id>/audio.wav``.

    Single source of truth for the adapter and the retention sweep
    (which recomputes the key from the call id and never parses a
    stored ``audio_url``).
    """
    return f"calls/{call_id}/audio.wav"


def s3_uri(bucket: str, key: str) -> str:
    """Stable ``s3://`` object URI (the webhook payload snapshot form)."""
    return f"s3://{bucket}/{key}"


class ObjectStorageClient(Protocol):
    """Storage seam every adapter implements.

    All methods are async even where the underlying SDK call is sync:
    blocking SDK calls are wrapped (``asyncio.to_thread``) inside the
    concrete adapter so the runner's event loop is never blocked.
    """

    async def upload_audio(self, call_id: uuid.UUID, file_path: Path) -> str:
        """Upload the WAV at ``file_path``; return the object URI.

        ``file_path`` is the bot's scratch WAV (``AUDIO_HOST_PATH/<call_id>.wav``).
        """
        ...

    async def delete_audio(self, call_id: uuid.UUID) -> None:
        """Delete the stored object; idempotent (missing object = no-op)."""
        ...

    async def presigned_url(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        """Time-limited presigned GET for retrieval.

        Audio is served only through presigned URLs — never public
        links (PR 7's provider fetch consumes this).
        """
        ...

    async def transcribable_source(self, call_id: uuid.UUID, *, ttl_seconds: int) -> AudioSource:
        """Build the ``AudioSource`` the transcription provider consumes.

        Transport strategy per adapter: S3 → presigned GET URL
        (provider fetches; ``ttl_seconds`` bounds the link) with the
        size from ``head_object``; local → the stored copy's own path
        (the provider never fetches; the transcription adapter POSTs
        the bytes) with the size from ``stat``.

        Raises ``SourceUnavailable`` (call-id-only message) when the
        stored object is gone or unreadable.
        """
        ...
