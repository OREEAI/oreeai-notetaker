"""Object-storage service — the thin facade workers call (PR 6).

The bot runner (post-upload seam) and the retention worker depend on
this class, never on a concrete adapter. The provider is selected once
per process by ``build_object_storage_service``:

- S3 vars set → ``S3ObjectStorageClient`` (any S3-API-compatible
  provider; env change, zero code).
- S3 vars unset in production → ``ConfigurationError`` (fail fast at
  startup; a runner that cannot store audio must not run).
- S3 vars unset in dev/staging → ``LocalObjectStorageClient`` (local
  filesystem stand-in under the scratch root) with a loud warning.
"""

import logging
import uuid
from pathlib import Path

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
    ObjectStorageClient,
)
from oreeai_notetaker.integrations.object_storage.local import LocalObjectStorageClient
from oreeai_notetaker.integrations.object_storage.s3 import S3ObjectStorageClient

logger = logging.getLogger(__name__)


class ObjectStorageService:
    """Facade over an ``ObjectStorageClient`` — what the workers call."""

    def __init__(self, client: ObjectStorageClient) -> None:
        self._client = client

    async def upload_for_call(self, call_id: uuid.UUID, file_path: Path) -> str:
        """Upload the call's WAV; returns the stable object URI."""
        return await self._client.upload_audio(call_id, file_path)

    async def delete_for_call(self, call_id: uuid.UUID) -> None:
        """Delete the call's stored object; idempotent."""
        await self._client.delete_audio(call_id)

    async def presigned_url_for_call(
        self, call_id: uuid.UUID, *, ttl_seconds: int | None = None
    ) -> str:
        """Time-limited presigned GET URL (default TTL from settings)."""
        ttl = ttl_seconds if ttl_seconds is not None else settings.s3_presign_ttl_seconds
        return await self._client.presigned_url(call_id, ttl_seconds=ttl)

    async def transcribable_source_for_call(
        self, call_id: uuid.UUID, *, ttl_seconds: int | None = None
    ) -> AudioSource:
        """Build the transcription provider's audio source (PR 7).

        Transport is the storage adapter's decision: S3 → presigned GET
        URL the provider fetches (TTL from ``S3_PRESIGN_TTL_SECONDS``);
        local dev fallback → its stored-copy path, POSTed as bytes.
        Raises ``SourceUnavailable`` when the stored object is gone.
        """
        ttl = ttl_seconds if ttl_seconds is not None else settings.s3_presign_ttl_seconds
        return await self._client.transcribable_source(call_id, ttl_seconds=ttl)


_storage_service: ObjectStorageService | None = None


def build_object_storage_service() -> ObjectStorageService:
    """Select and build the storage service for this process (cached).

    Built once at runner startup so a broken storage configuration fails
    fast (``ConfigurationError``) instead of at first upload.
    """
    global _storage_service
    if _storage_service is not None:
        return _storage_service
    if settings.s3_bucket:
        service = ObjectStorageService(S3ObjectStorageClient.from_settings())
    elif settings.environment == "production":
        raise ConfigurationError(
            "S3_BUCKET is not configured: production cannot upload audio without object storage"
        )
    else:
        client = LocalObjectStorageClient.from_settings()
        logger.warning(
            "S3 vars unset; using local object storage under %s (dev/staging only)", client.root
        )
        service = ObjectStorageService(client)
    _storage_service = service
    return service


def reset_object_storage_service() -> None:
    """Drop the cached service (test seam; the runner never resets)."""
    global _storage_service
    _storage_service = None
