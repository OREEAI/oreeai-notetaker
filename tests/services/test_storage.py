"""``ObjectStorageService`` facade + provider selection (PR 6).

Selection contract: S3 vars set → S3 adapter (moto proves it); unset in
production → fail-fast ``ConfigurationError``; unset in dev/staging →
the local filesystem stand-in.
"""

import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    ConfigurationError,
    audio_key,
)
from oreeai_notetaker.services.storage import (
    build_object_storage_service,
    reset_object_storage_service,
)

BUCKET = "oreeai-facade-bucket"


@pytest.fixture(autouse=True)
def _fresh_storage_singleton() -> None:
    reset_object_storage_service()
    yield
    reset_object_storage_service()


def s3_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_region", "us-east-1")
    monkeypatch.setattr(settings, "s3_access_key_id", "testing")
    monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
    monkeypatch.setattr(settings, "s3_endpoint_url", None)
    monkeypatch.setattr(settings, "s3_sse", "AES256")


def local_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "s3_bucket", None)
    monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))


async def test_s3_adapter_selected_when_bucket_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        s3_settings(monkeypatch)
        service = build_object_storage_service()

        call_id = uuid.uuid4()
        source = tmp_path / f"{call_id}.wav"
        source.write_bytes(b"RIFF-fake")
        uri = await service.upload_for_call(call_id, source)

        assert uri == f"s3://{BUCKET}/calls/{call_id}/audio.wav"
        head = client.head_object(Bucket=BUCKET, Key=audio_key(call_id))
        assert head["ServerSideEncryption"] == "AES256"
        assert head["ContentLength"] == source.stat().st_size


async def test_dev_without_s3_uses_local_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_settings(monkeypatch, tmp_path)
    service = build_object_storage_service()

    call_id = uuid.uuid4()
    source = tmp_path / f"{call_id}.wav"
    source.write_bytes(b"RIFF-fake")
    uri = await service.upload_for_call(call_id, source)

    stored = tmp_path / "objects" / audio_key(call_id)
    assert uri == f"file://{stored.resolve()}"
    assert stored.exists()


def test_production_without_s3_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "s3_bucket", None)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(ConfigurationError, match="production"):
        build_object_storage_service()


def test_service_is_cached_per_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_settings(monkeypatch, tmp_path)
    first = build_object_storage_service()
    second = build_object_storage_service()
    assert first is second
