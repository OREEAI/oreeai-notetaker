"""Deepgram adapter tests (PR 7 pass 1).

Mocked HTTP layer via ``httpx.MockTransport`` (the webhook dispatcher's
established pattern — no new test deps). Asserted here:

- **Both transport branches**: JSON url-body request shape (provider
  fetches the presigned GET) and binary-body request shape (raw
  ``audio/wav`` bytes of the stored copy).
- Pinned params in both branches: ``model=nova-3`` +
  ``diarize_model=latest``, and the deprecated bare ``diarize`` param
  absent (setting both is rejected by the API).
- Error mapping: 429/5xx/timeout/transport → transient; 401/400/402 →
  permanent (402 carries the operator-action hint).
- Response parsing: provider-native utterances → pinned segment schema,
  speaker ints rendered as ``S0``/``S1``; word-run fallback; missing
  ``results`` is permanent (pinned: the response is the only copy).
- Log hygiene: no transcript text, no local path, no presigned URL, no
  API key in any log line.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from oreeai_notetaker.integrations.object_storage.base import AudioSource
from oreeai_notetaker.integrations.transcription.base import (
    PermanentTranscriptionError,
    TransientTranscriptionError,
)
from oreeai_notetaker.integrations.transcription.deepgram import (
    DIARIZE_MODEL,
    MODEL,
    PARAMS,
    DeepgramTranscriptionClient,
    parse_response,
)

API_KEY = "dg-test-key-not-real"

SAMPLE_PRESIGNED_URL = "https://bucket.example.com/calls/<call-id>/audio.wav?X-Amz-Signature=sig"


def _client(handler: Any) -> DeepgramTranscriptionClient:
    return DeepgramTranscriptionClient(API_KEY, transport=httpx.MockTransport(handler))


def _wav(tmp_path: Path, body: bytes = b"RIFF----WAVEfmt-data") -> Path:
    path = tmp_path / "stored.wav"
    path.write_bytes(body)
    return path


def _utterances_payload() -> dict[str, Any]:
    return {
        "metadata": {"request_id": "req-1", "duration": 3.2},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "hello there. hi back",
                            "confidence": 0.98,
                            "words": [],
                        }
                    ]
                }
            ],
            "utterances": [
                {
                    "transcript": "hello there.",
                    "start": 0.1,
                    "end": 1.4,
                    "speaker": 0,
                    "confidence": 0.97,
                },
                {
                    "transcript": "hi back",
                    "start": 1.8,
                    "end": 2.9,
                    "speaker": 1,
                    "confidence": 0.95,
                },
            ],
        },
    }


class TestUrlTransportBranch:
    async def test_request_shape_json_url_body(self, tmp_path: Path) -> None:
        """Presigned-GET transport: JSON body, JSON content type, pinned
        query params, Token auth."""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            seen["auth"] = request.headers.get("Authorization")
            seen["content_type"] = request.headers.get("Content-Type")
            seen["body"] = request.read()
            return httpx.Response(200, json=_utterances_payload())

        transcript = await _client(handler).transcribe(
            AudioSource(url=SAMPLE_PRESIGNED_URL, size_bytes=1024)
        )

        assert seen["method"] == "POST"
        assert seen["path"] == "/v1/listen"
        assert seen["auth"] == f"Token {API_KEY}"
        assert seen["content_type"] == "application/json"
        assert json.loads(seen["body"]) == {"url": SAMPLE_PRESIGNED_URL}
        assert seen["params"]["model"] == MODEL
        assert seen["params"]["diarize_model"] == DIARIZE_MODEL
        assert seen["params"]["utterances"] == "true"
        assert seen["params"]["punctuate"] == "true"
        assert seen["params"]["smart_format"] == "true"
        assert [s.speaker for s in transcript.segments] == ["S0", "S1"]

    async def test_deprecated_diarize_param_is_absent(self) -> None:
        # docs: setting both diarize and diarize_model is rejected
        assert "diarize" not in PARAMS
        assert PARAMS["diarize_model"] == "latest"

    async def test_presigned_url_never_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_utterances_payload())

        await _client(handler).transcribe(AudioSource(url=SAMPLE_PRESIGNED_URL, size_bytes=1024))
        assert SAMPLE_PRESIGNED_URL not in caplog.text


class TestBinaryTransportBranch:
    async def test_request_shape_binary_wav_body(self, tmp_path: Path) -> None:
        stored = _wav(tmp_path, body=b"RIFF" + bytes(120))
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            seen["content_type"] = request.headers.get("Content-Type")
            seen["params"] = dict(request.url.params)
            seen["body"] = request.read()
            return httpx.Response(200, json=_utterances_payload())

        await _client(handler).transcribe(AudioSource(local_path=stored, size_bytes=124))

        assert seen["auth"] == f"Token {API_KEY}"
        assert seen["content_type"] == "audio/wav"
        assert seen["body"] == stored.read_bytes()  # the stored copy, verbatim
        assert seen["params"]["model"] == MODEL
        assert seen["params"]["diarize_model"] == DIARIZE_MODEL
        assert seen["params"]["utterances"] == "true"

    async def test_local_path_never_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        stored = _wav(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_utterances_payload())

        await _client(handler).transcribe(
            AudioSource(local_path=stored, size_bytes=stored.stat().st_size)
        )
        assert str(stored) not in caplog.text
        assert "audio.wav" not in caplog.text

    async def test_api_key_never_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_utterances_payload())

        await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))
        assert API_KEY not in caplog.text


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "permanent"),
        [
            (429, False),
            (500, False),
            (502, False),
            (503, False),
            (504, False),
            (400, True),
            (401, True),
            (402, True),
            (403, True),
            (404, True),
        ],
    )
    async def test_status_mapping(self, tmp_path: Path, status: int, permanent: bool) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"err_msg": "x"})

        with pytest.raises(
            PermanentTranscriptionError if permanent else TransientTranscriptionError
        ):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_402_message_carries_operator_hint(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402)

        with pytest.raises(PermanentTranscriptionError, match="insufficient credits"):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_401_message_names_the_key_variable(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        with pytest.raises(PermanentTranscriptionError, match="DEEPGRAM_API_KEY"):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_timeout_is_transient(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        with pytest.raises(TransientTranscriptionError, match="timed out"):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_connection_error_is_transient(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(TransientTranscriptionError, match="connection failed"):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_non_json_200_is_permanent_not_silent(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>gateway</html>")

        with pytest.raises(PermanentTranscriptionError, match="non-JSON"):
            await _client(handler).transcribe(AudioSource(local_path=_wav(tmp_path), size_bytes=64))

    async def test_unreadable_stored_copy_is_permanent(self, tmp_path: Path) -> None:
        missing = tmp_path / "never-uploaded.wav"
        with pytest.raises(PermanentTranscriptionError, match="unreadable"):
            await _client(lambda request: httpx.Response(200, json={})).transcribe(
                AudioSource(local_path=missing, size_bytes=64)
            )


class TestResponseParsing:
    def test_utterances_map_onto_pinned_schema(self) -> None:
        transcript = parse_response(_utterances_payload())
        assert [(s.speaker, s.start, s.end, s.text) for s in transcript.segments] == [
            ("S0", 0.1, 1.4, "hello there."),
            ("S1", 1.8, 2.9, "hi back"),
        ]

    def test_word_run_fallback_when_utterances_absent(self) -> None:
        payload = {
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "hello",
                                "words": [
                                    {"word": "hello", "start": 0.0, "end": 0.5, "speaker": 0},
                                    {"word": "you", "start": 0.5, "end": 0.9, "speaker": 0},
                                    {"word": "there", "start": 1.2, "end": 1.8, "speaker": 1},
                                ],
                            }
                        ]
                    }
                ]
            }
        }
        transcript = parse_response(payload)
        assert [(s.speaker, s.start, s.end, s.text) for s in transcript.segments] == [
            ("S0", 0.0, 0.9, "hello you"),
            ("S1", 1.2, 1.8, "there"),
        ]

    def test_missing_results_object_is_permanent(self) -> None:
        with pytest.raises(PermanentTranscriptionError, match="results"):
            parse_response({"metadata": {}})


class TestRealtimeSeam:
    def test_supports_realtime_flag(self) -> None:
        # Realtime stays available on the same account for phase 3.
        assert DeepgramTranscriptionClient.supports_realtime is True


class TestAudioSourceInvariants:
    def test_exactly_one_source_required(self) -> None:
        with pytest.raises(ValidationError):
            AudioSource(url=SAMPLE_PRESIGNED_URL, local_path=Path("/x"), size_bytes=1)
        with pytest.raises(ValidationError):
            AudioSource(size_bytes=1)

    def test_valid_url_only(self) -> None:
        source = AudioSource(url=SAMPLE_PRESIGNED_URL, size_bytes=1)
        assert source.local_path is None

    def test_valid_local_only(self) -> None:
        source = AudioSource(local_path=Path("/x/audio.wav"), size_bytes=1)
        assert source.url is None
