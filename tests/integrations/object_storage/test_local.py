"""Local dev/staging storage adapter (PR 6).

Same contract as the S3 adapter against the local filesystem: pinned
key layout under the scratch root, stable ``file://`` URI, idempotent
delete, missing-source uploads fail typed. Never used in production —
``build_object_storage_service`` fail-fasts there instead.
"""

import uuid
from pathlib import Path

import pytest

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    UploadFailed,
    audio_key,
)
from oreeai_notetaker.integrations.object_storage.local import LocalObjectStorageClient


async def test_upload_delete_presign_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
    adapter = LocalObjectStorageClient.from_settings()
    assert adapter.root == tmp_path / "objects"

    call_id = uuid.uuid4()
    source = tmp_path / f"{call_id}.wav"
    source.write_bytes(b"RIFF-fake")

    uri = await adapter.upload_audio(call_id, source)
    stored = tmp_path / "objects" / audio_key(call_id)
    assert uri == f"file://{stored.resolve()}"
    assert stored.read_bytes() == source.read_bytes()

    await adapter.delete_audio(call_id)
    assert not stored.exists()
    await adapter.delete_audio(call_id)  # idempotent

    url = await adapter.presigned_url(call_id, ttl_seconds=60)
    assert url == f"file://{stored.resolve()}"


async def test_missing_source_raises_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
    adapter = LocalObjectStorageClient.from_settings()
    call_id = uuid.uuid4()

    with pytest.raises(UploadFailed) as excinfo:
        await adapter.upload_audio(call_id, tmp_path / "missing.wav")

    assert str(call_id) in str(excinfo.value)
