"""Local-filesystem storage — the dev/staging stand-in (never production).

Implements the same ``ObjectStorageClient`` contract against the local
disk so the runner's full flow (upload → ``audio_url`` → retention →
scratch delete) exercises identically without a bucket or credentials.
``build_object_storage_service`` selects it only when the S3 vars are
unset and ``ENVIRONMENT != "production"`` (production fails fast at
startup instead).

Not for production: ``presigned_url`` returns the ``file://`` location
directly — the presigned-only serving rule is an S3-provider concern,
and there is no bucket boundary here to enforce it.
"""

import asyncio
import shutil
import uuid
from pathlib import Path

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    UploadFailed,
    audio_key,
)


class LocalObjectStorageClient:
    """``ObjectStorageClient`` over a local directory tree."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def from_settings(cls) -> "LocalObjectStorageClient":
        # Lives under the existing scratch root (AUDIO_HOST_PATH) so no
        # new env var is introduced; the runner mkdirs that root
        # best-effort at startup.
        return cls(Path(settings.audio_host_path) / "objects")

    async def upload_audio(self, call_id: uuid.UUID, file_path: Path) -> str:
        """Copy the scratch WAV to ``<root>/calls/<call_id>/audio.wav``."""
        target = self.root / audio_key(call_id)
        try:
            await asyncio.to_thread(self._copy, file_path, target)
        except OSError as exc:
            raise UploadFailed(f"local upload failed for call {call_id}") from exc
        return f"file://{target.resolve()}"

    async def delete_audio(self, call_id: uuid.UUID) -> None:
        # unlink(missing_ok=True): idempotent, matching the S3 contract.
        target = self.root / audio_key(call_id)
        await asyncio.to_thread(_unlink_missing_ok, target)

    async def presigned_url(self, call_id: uuid.UUID, *, ttl_seconds: int) -> str:
        # Dev/test stand-in: no TTL enforcement — the caller gets the
        # plain file location. Production always uses the S3 adapter.
        target = self.root / audio_key(call_id)
        return f"file://{target.resolve()}"

    def _copy(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _unlink_missing_ok(path: Path) -> None:
    path.unlink(missing_ok=True)
