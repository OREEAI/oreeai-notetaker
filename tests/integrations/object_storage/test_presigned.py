"""Presigned-only serving rule (PR 6 standing rule, CI tier).

A presigned GET succeeds within the TTL and returns the stored bytes;
an unauthenticated GET on the private object fails. Uses moto's HTTP
server (the in-memory moto mock cannot serve requests) on an ephemeral
loopback port — still no docker daemon, CI-safe. The real-provider
rerun of this matrix is manual scenario 2 (``head-object`` SSE +
provider endpoint).
"""

import uuid
from pathlib import Path

import boto3
import httpx
import pytest
from moto.moto_server import threaded_moto_server

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.integrations.object_storage.base import audio_key
from oreeai_notetaker.integrations.object_storage.s3 import S3ObjectStorageClient

BUCKET = "oreeai-presign-bucket"


@pytest.fixture
def moto_endpoint(monkeypatch: pytest.MonkeyPatch):
    server = threaded_moto_server.ThreadedMotoServer("127.0.0.1", 0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    monkeypatch.setattr(settings, "s3_endpoint_url", endpoint)
    monkeypatch.setattr(settings, "s3_region", "us-east-1")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_access_key_id", "testing")
    monkeypatch.setattr(settings, "s3_secret_access_key", "testing")
    monkeypatch.setattr(settings, "s3_sse", "AES256")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    client.create_bucket(Bucket=BUCKET)
    yield endpoint
    server.stop()


async def test_presigned_get_succeeds_unauth_get_is_rejected(
    moto_endpoint: str, tmp_path: Path
) -> None:
    call_id = uuid.uuid4()
    payload = b"RIFF" + b"\x00" * 512
    source = tmp_path / f"{call_id}.wav"
    source.write_bytes(payload)
    adapter = S3ObjectStorageClient.from_settings()

    await adapter.upload_audio(call_id, source)

    url = await adapter.presigned_url(call_id, ttl_seconds=60)
    assert url.startswith(moto_endpoint)

    async with httpx.AsyncClient() as http:
        presigned = await http.get(url)
        unauthenticated = await http.get(f"{moto_endpoint}/{BUCKET}/{audio_key(call_id)}")

    assert presigned.status_code == 200
    assert presigned.content == payload
    assert unauthenticated.status_code == 403
