"""Moto-backed tests for the S3 adapter (PR 6 pass 1).

Mirrors of the "You test this" scenarios that CI allows: SSE header
requested on upload, pinned key layout, stable ``s3://`` URI, idempotent
delete, multipart above 8 MB, typed errors, and the log-hygiene rule —
no audio bytes, no object keys, no ``user_ref`` in storage log output.

The real-provider checks (``head-object`` showing
``ServerSideEncryption``, unauth 403 vs presigned 200) stay manual per
the chunk; moto proves the request shape, the HTTP presigned/unauth
matrix runs against moto's server in ``test_presigned.py``.
"""

import uuid
from pathlib import Path

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
from oreeai_notetaker.integrations.object_storage.s3 import S3ObjectStorageClient

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
    call_id = uuid.uuid4()
    size = 9 * 1024 * 1024
    adapter = S3ObjectStorageClient.from_settings()

    await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id, size))

    head = s3_env.head_object(Bucket=BUCKET, Key=audio_key(call_id))
    assert head["ContentLength"] == size


async def test_upload_failure_raises_typed_keyfree_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_region", "us-east-1")
    monkeypatch.setattr(settings, "s3_access_key_id", "testing")
    monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
    # Unroutable port: connection fails fast, no moto interception.
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "s3_sse", "AES256")
    adapter = S3ObjectStorageClient.from_settings()
    call_id = uuid.uuid4()

    with pytest.raises(UploadFailed) as excinfo:
        await adapter.upload_audio(call_id, stage_wav(tmp_path, call_id))

    assert str(call_id) in str(excinfo.value)
    # botocore errors embed the object key; the raised message must not.
    assert "audio.wav" not in str(excinfo.value)


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
