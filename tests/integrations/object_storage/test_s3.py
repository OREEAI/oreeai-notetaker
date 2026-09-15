"""Moto-backed tests for the S3 adapter (PR 6 pass 1).

Mirrors of the "You test this" scenarios that CI allows: startup SSE
probe (typed ``ConfigurationError``, not a runtime error), SSE header
requested on upload, pinned key layout, stable ``s3://`` URI, idempotent
delete, multipart above 8 MB, typed errors, and the log-hygiene rule —
no audio bytes, no object keys, no ``user_ref``, no ``webhook_secret``
in storage log output.

The real-provider checks (``head-object`` showing
``ServerSideEncryption``, unauth 403 vs presigned 200) stay manual per
the chunk; moto proves the request shape, the HTTP presigned/unauth
matrix runs against moto's server in ``test_presigned.py``.
"""

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    ConfigurationError,
    UploadFailed,
    audio_key,
)
from oreeai_notetaker.integrations.object_storage.s3 import (
    MULTIPART_CHUNKSIZE_BYTES,
    MULTIPART_THRESHOLD_BYTES,
    S3ObjectStorageClient,
)

BUCKET = "oreeai-test-bucket"


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch):
    """Moto-backed bucket with settings pointed at it."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(settings, "s3_bucket", BUCKET)
        monkeypatch.setattr(settings, "s3_region", "us-east-1")
        monkeypatch.setattr(settings, "s3_access_key_id", "testing")
        monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
        monkeypatch.setattr(settings, "s3_endpoint_url", None)
        monkeypatch.setattr(settings, "s3_sse", "AES256")
        yield client


def stage_wav(tmp_path: Path, call_id: uuid.UUID, size: int = 1024) -> Path:
    path = tmp_path / f"{call_id}.wav"
    path.write_bytes(b"RIFF" + b"\x00" * (size - 4))
    return path


def test_missing_bucket_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "s3_bucket", None)
    with pytest.raises(ConfigurationError, match="S3_BUCKET"):
        S3ObjectStorageClient.from_settings()


def test_missing_credentials_raise_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_access_key_id", None)
    with pytest.raises(ConfigurationError, match="S3_ACCESS_KEY_ID"):
        S3ObjectStorageClient.from_settings()


def test_probe_key_is_pid_suffixed(s3_env) -> None:
    # Two processes building concurrently must not share one probe key.
    captured: dict[str, str] = {}

    adapter = S3ObjectStorageClient.from_settings()
    original_put = adapter._client.put_object

    def spy(**kwargs: object) -> object:
        captured["key"] = str(kwargs.get("Key"))
        return original_put(**kwargs)

    with patch.object(adapter._client, "put_object", side_effect=spy):
        adapter.startup_sse_probe()

    assert captured["key"].startswith(f".oreeai-startup-probe-{os.getpid()}")


def test_unreachable_endpoint_surfaces_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chunk edge case: an unusable bucket is a config error at startup
    # (the probe), not a runtime error on the first upload.
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_region", "us-east-1")
    monkeypatch.setattr(settings, "s3_access_key_id", "testing")
    monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "s3_sse", "AES256")
    with pytest.raises(ConfigurationError, match="startup probe"):
        S3ObjectStorageClient.from_settings()


async def test_upload_uses_pinned_key_layout_sse_and_stable_uri(
    s3_env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    call_id = uuid.uuid4()
    adapter = S3ObjectStorageClient.from_settings()

    uri = await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id))

    assert uri == f"s3://{BUCKET}/calls/{call_id}/audio.wav"
    head = s3_env.head_object(Bucket=BUCKET, Key=audio_key(call_id))
    assert head["ServerSideEncryption"] == "AES256"
    assert "user_ref" not in caplog.text
    assert "audio.wav" not in caplog.text
    assert "webhook_secret" not in caplog.text


async def test_sse_value_follows_config(
    s3_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "s3_sse", "aws:kms")
    call_id = uuid.uuid4()
    adapter = S3ObjectStorageClient.from_settings()

    await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id))

    head = s3_env.head_object(Bucket=BUCKET, Key=audio_key(call_id))
    assert head["ServerSideEncryption"] == "aws:kms"


async def test_delete_is_idempotent(s3_env, tmp_path: Path) -> None:
    call_id = uuid.uuid4()
    adapter = S3ObjectStorageClient.from_settings()
    await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id))

    await adapter.delete_audio(call_id)
    await adapter.delete_audio(call_id)  # missing object: no-op success

    with pytest.raises(ClientError):
        s3_env.head_object(Bucket=BUCKET, Key=audio_key(call_id))


async def test_multipart_above_8mb_threshold(s3_env, tmp_path: Path) -> None:
    assert MULTIPART_THRESHOLD_BYTES == 8 * 1024 * 1024
    assert MULTIPART_CHUNKSIZE_BYTES == 8 * 1024 * 1024
    call_id = uuid.uuid4()
    size = 9 * 1024 * 1024
    adapter = S3ObjectStorageClient.from_settings()
    # Pin the multipart path itself: above the threshold upload_file must
    # coordinate a multipart upload, not a single put_object.
    with patch.object(
        adapter._client,
        "create_multipart_upload",
        wraps=adapter._client.create_multipart_upload,
    ) as multipart_spy:
        await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id, size))

    assert multipart_spy.called
    head = s3_env.head_object(Bucket=BUCKET, Key=audio_key(call_id))
    assert head["ContentLength"] == size


async def test_mid_multipart_failure_raises_typed_keyfree_error(
    s3_env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    call_id = uuid.uuid4()
    adapter = S3ObjectStorageClient.from_settings()
    failure = ClientError(
        {"Error": {"Code": "InternalError", "Message": "part failed"}}, "UploadPart"
    )
    with (
        patch.object(adapter._client, "upload_part", side_effect=failure),
        pytest.raises(UploadFailed) as excinfo,
    ):
        await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id, 9 * 1024 * 1024))

    # s3transfer wraps mid-transfer failures as S3UploadFailedError, whose
    # message embeds the local path AND the object key — the adapter must
    # swallow it into a call-id-only typed error.
    assert str(call_id) in str(excinfo.value)
    assert "audio.wav" not in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)
    assert "user_ref" not in caplog.text
    assert "audio.wav" not in caplog.text
