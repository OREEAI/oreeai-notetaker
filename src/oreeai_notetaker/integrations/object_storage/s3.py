"""S3-API adapter — one code path for every S3-compatible provider.

Built purely from settings: ``S3_ENDPOINT_URL`` / ``S3_REGION`` /
``S3_BUCKET`` / ``S3_ACCESS_KEY_ID`` / ``S3_SECRET_ACCESS_KEY`` /
``S3_SSE``. Switching provider (AWS, Cloudflare R2, Hetzner Object
Storage, Backblaze B2, MinIO) is an env change, zero code.

Adapter choices that keep the provider list interchangeable:

- **Signature v4** (``signature_version="s3v4"``) presigning against the
  configured endpoint — works identically on R2/Hetzner/Backblaze/MinIO.
- **Path-style addressing** (``s3={"addressing_style": "path"}``) —
  virtual-hosted-style presigned URLs are unreliable on S3-compatible
  providers (R2/B2/MinIO); path-style is what all of them document.
- **Multipart above 8 MB** via ``TransferConfig`` — the ~115 MB/hour
  recording (PR 1 audio contract) uploads in bounded chunks.
- **Server-side encryption** from ``S3_SSE`` (default ``AES256``) is
  requested on every upload; a bucket that rejects the header surfaces
  as ``UploadFailed`` on the first call (the real per-provider SSE
  check is a manual ``head-object``, README: Data retention).

The client is blocking boto3 wrapped in ``asyncio.to_thread``: the
runner's event loop never waits on it inline.
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    ConfigurationError,
    DeleteFailed,
    UploadFailed,
    audio_key,
    s3_uri,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)

MULTIPART_THRESHOLD_BYTES = 8 * 1024 * 1024
MULTIPART_CHUNKSIZE_BYTES = 8 * 1024 * 1024


class S3ObjectStorageClient:
    """``ObjectStorageClient`` over any S3-API-compatible endpoint."""

    def __init__(self, client: "S3Client", bucket: str, sse: str) -> None:
        self._client = client
        self._bucket = bucket
        self._sse = sse

    @classmethod
    def from_settings(cls) -> "S3ObjectStorageClient":
        """Build from ``settings``; raise ``ConfigurationError`` at startup
        when the required vars are unset (runner fail-fast path)."""
        if not settings.s3_bucket:
            raise ConfigurationError("S3_BUCKET is not configured")
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise ConfigurationError("S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY are not configured")
        session = boto3.Session(
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            # SigV4 needs a region; S3-compatible providers override it
            # via S3_REGION (R2 wants "auto") and this is the AWS fallback.
            region_name=settings.s3_region or "us-east-1",
        )
        client = session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            config=BotocoreConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.s3_bucket, settings.s3_sse)

    async def upload_audio(self, call_id: uuid.UUID, file_path: Path) -> str:
        key = audio_key(call_id)
        extra_args = {"ServerSideEncryption": self._sse} if self._sse else {}
        try:
            await asyncio.to_thread(
                self._client.upload_file,
                str(file_path),
                self._bucket,
                key,
                ExtraArgs=extra_args,
                Config=TransferConfig(
                    multipart_threshold=MULTIPART_THRESHOLD_BYTES,
                    multipart_chunksize=MULTIPART_CHUNKSIZE_BYTES,
                ),
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            # Chain the cause (traceback-only, for operators) but keep the
            # raised message key-free: botocore errors embed the object
            # key, and storage log lines must never carry it.
            raise UploadFailed(f"s3 upload failed for call {call_id}") from exc
        return s3_uri(self._bucket, key)

    async def delete_audio(self, call_id: uuid.UUID) -> None:
        # S3 delete on a missing key is a success (204): idempotent by
        # contract — missing objects never crash the retention sweep.
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=audio_key(call_id)
            )
        except (BotoCoreError, ClientError) as exc:
            raise DeleteFailed(f"s3 delete failed for call {call_id}") from exc

    async def presigned_url(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        # Presigning is local computation (no network I/O); to_thread keeps
        # the async contract uniform across adapters.
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": audio_key(call_id)},
            ExpiresIn=ttl_seconds,
        )
