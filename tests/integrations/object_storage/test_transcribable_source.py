"""``transcribable_source`` seam tests (PR 7 pass 1).

The hybrid-transport amendment: the storage adapter owns the transport
strategy. S3 → presigned GET URL the provider fetches (TTL honored,
size from ``head_object``); local → the stored-copy path the
transcription adapter POSTs as bytes (size from ``stat``). Missing
stored objects raise typed ``SourceUnavailable`` with call-id-only
messages (no object keys, no local paths in log output).
"""

import logging
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import (
    SourceUnavailable,
    audio_key,
    s3_uri,
)
from oreeai_notetaker.integrations.object_storage.local import LocalObjectStorageClient
from oreeai_notetaker.integrations.object_storage.s3 import S3ObjectStorageClient

BUCKET = "oreeai-test-bucket"


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(settings, "s3_bucket", BUCKET)
        monkeypatch.setattr(settings, "s3_region", "us-east-1")
        monkeypatch.setattr(settings, "s3_access_key_id", "testing")
        monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
        monkeypatch.setattr(settings, "s3_endpoint_url", None)
        monkeypatch.setattr(settings, "s3_sse", "AES256")
        monkeypatch.setattr(settings, "s3_presign_ttl_seconds", 3600)
        yield client


class TestLocalTranscribableSource:
    async def test_stored_copy_path_and_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        adapter = LocalObjectStorageClient.from_settings()
        call_id = uuid.uuid4()
        scratch = tmp_path / f"{call_id}.wav"
        scratch.write_bytes(b"RIFF" + b"\x00" * 996)
        await adapter.upload_audio(call_id, scratch)

        source = await adapter.transcribable_source(call_id, ttl_seconds=3600)

        assert source.url is None
        # the stored copy (what the file:// URI points at), not the scratch
        assert source.local_path == tmp_path / "objects" / audio_key(call_id)
        assert source.size_bytes == 1000

    async def test_missing_stored_copy_raises_source_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(settings, "audio_host_path", str(tmp_path))
        adapter = LocalObjectStorageClient.from_settings()
        call_id = uuid.uuid4()
        with pytest.raises(SourceUnavailable, match=str(call_id)):
            await adapter.transcribable_source(call_id, ttl_seconds=3600)
        # never the local path in logs (standing rule)
        caplog.set_level(logging.DEBUG)
        assert "audio.wav" not in caplog.text


class TestS3TranscribableSource:
    async def test_presigned_url_transport_and_size(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            monkeypatch.setattr(settings, "s3_bucket", BUCKET)
            monkeypatch.setattr(settings, "s3_region", "us-east-1")
            monkeypatch.setattr(settings, "s3_access_key_id", "testing")
            monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
            monkeypatch.setattr(settings, "s3_endpoint_url", None)
            monkeypatch.setattr(settings, "s3_sse", "AES256")
            monkeypatch.setattr(settings, "s3_presign_ttl_seconds", 3600)
            adapter = S3ObjectStorageClient.from_settings()
            call_id = uuid.uuid4()
            scratch = tmp_path / f"{call_id}.wav"
            scratch.write_bytes(b"RIFF" + b"\x00" * 1020)
            uri = await adapter.upload_audio(call_id, scratch)
            assert uri == s3_uri(BUCKET, audio_key(call_id))

            source = await adapter.transcribable_source(call_id, ttl_seconds=3600)

            # presigned-only serving: a fetchable https URL, never file://
            assert source.url is not None
            assert source.url.startswith("https://") or source.url.startswith("http://")
            assert "X-Amz-Signature=" in source.url, "presigned, time-limited"
            assert source.local_path is None
            assert source.size_bytes == 1024

    async def test_vanished_object_raises_source_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            monkeypatch.setattr(settings, "s3_bucket", BUCKET)
            monkeypatch.setattr(settings, "s3_region", "us-east-1")
            monkeypatch.setattr(settings, "s3_access_key_id", "testing")
            monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
            monkeypatch.setattr(settings, "s3_endpoint_url", None)
            monkeypatch.setattr(settings, "s3_sse", "AES256")
            adapter = S3ObjectStorageClient.from_settings()
            call_id = uuid.uuid4()
            with pytest.raises(SourceUnavailable, match=str(call_id)):
                await adapter.transcribable_source(call_id, ttl_seconds=3600)
        caplog.set_level(logging.DEBUG)
        assert "audio.wav" not in caplog.text, "object keys never logged"
