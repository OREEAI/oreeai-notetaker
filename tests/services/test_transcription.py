"""Transcription service tests (PR 7 pass 1).

Covers the chunk's policy layer: provider selection (the env matrix
mirroring the storage fail-fast), the ``AUDIO_MAX_BYTES`` size guard,
transient retry (3 attempts, 1/4/16 s backoff), immediate permanent
failure, and the stub provider's honest-empty behavior. Caplog mirrors
assert the standing rules: transcript text never in logs (only
lengths/counts), never the local audio path.
"""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from oreeai_notetaker.core.config import Settings, settings
from oreeai_notetaker.integrations.object_storage.base import (
    AudioSource,
    ConfigurationError,
)
from oreeai_notetaker.integrations.transcription.base import (
    AudioTooLarge,
    PermanentTranscriptionError,
    SpeakerSegment,
    Transcript,
    TransientTranscriptionError,
)
from oreeai_notetaker.integrations.transcription.deepgram import (
    DeepgramTranscriptionClient,
)
from oreeai_notetaker.integrations.transcription.stub import StubTranscriptionClient
from oreeai_notetaker.services.transcription import (
    RETRY_ATTEMPTS,
    TranscriptionService,
    build_transcription_service,
    reset_transcription_service,
)


@pytest.fixture(autouse=True)
def _fresh_service_cache() -> Any:
    reset_transcription_service()
    yield
    reset_transcription_service()


def url_source(size_bytes: int = 1024) -> AudioSource:
    return AudioSource(
        url="https://bucket.example.com/calls/<id>/audio.wav?sig=1", size_bytes=size_bytes
    )


def local_source(tmp_path: Any, size_bytes: int = 1024) -> AudioSource:
    return AudioSource(local_path=tmp_path / "stored.wav", size_bytes=size_bytes)


def one_segment(text: str = "ok") -> Transcript:
    return Transcript(segments=[SpeakerSegment(speaker="S0", start=0.0, end=1.0, text=text)])


class RecorderClient:
    """Minimal ``TranscriptionClient`` stand-in with scripted behavior."""

    def __init__(
        self,
        outcomes: list[Any] | None = None,
        *,
        permanent: bool = False,
    ) -> None:
        self.calls = 0
        self.sources: list[AudioSource] = []
        self.outcomes = outcomes or []
        self.permanent = permanent

    async def transcribe(self, source: AudioSource) -> Transcript:
        self.calls += 1
        self.sources.append(source)
        if self.permanent:
            raise PermanentTranscriptionError("deepgram returned HTTP 401")
        outcomes = self.outcomes
        if not outcomes:
            raise TransientTranscriptionError("deepgram returned HTTP 429")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestProviderSelection:
    async def test_deepgram_selected_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "transcription_provider", "deepgram")
        monkeypatch.setattr(settings, "deepgram_api_key", "dg-key")
        service = build_transcription_service()
        assert isinstance(service._client, DeepgramTranscriptionClient)

    async def test_deepgram_without_key_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "transcription_provider", "deepgram")
        monkeypatch.setattr(settings, "deepgram_api_key", None)
        with pytest.raises(ConfigurationError, match="DEEPGRAM_API_KEY"):
            build_transcription_service()

    async def test_stub_selected_explicitly_warns_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "transcription_provider", "stub")
        with caplog.at_level(logging.WARNING):
            service = build_transcription_service()
        assert isinstance(service._client, StubTranscriptionClient)
        assert "stub" in caplog.text and "EMPTY transcript" in caplog.text

    async def test_stub_in_production_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "transcription_provider", "stub")
        monkeypatch.setattr(settings, "environment", "production")
        with pytest.raises(ConfigurationError, match="stub"):
            build_transcription_service()

    async def test_unset_in_production_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "transcription_provider", None)
        monkeypatch.setattr(settings, "environment", "production")
        with pytest.raises(ConfigurationError, match="TRANSCRIPTION_PROVIDER"):
            build_transcription_service()

    async def test_unset_in_dev_falls_back_to_stub_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(settings, "transcription_provider", None)
        with caplog.at_level(logging.WARNING):
            service = build_transcription_service()
        assert isinstance(service._client, StubTranscriptionClient)
        assert "TRANSCRIPTION_PROVIDER unset" in caplog.text
        assert "EMPTY transcript" in caplog.text

    async def test_unknown_provider_value_rejected_by_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Typo fail-fast in BOTH environments: the Literal validator runs
        # at settings load, before any factory gets involved.
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "deepgramm")
        with pytest.raises(ValidationError, match="transcription_provider"):
            Settings(api_key="test-key")
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(ValidationError, match="transcription_provider"):
            Settings(api_key="test-key")

    async def test_service_is_cached_until_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "transcription_provider", "stub")
        first = build_transcription_service()
        assert build_transcription_service() is first
        reset_transcription_service()
        second = build_transcription_service()
        assert second is not first


class TestSizeGuard:
    async def test_over_limit_refused_before_any_request(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(settings, "audio_max_bytes", 1000)
        client = RecorderClient()
        service = TranscriptionService(client)
        with pytest.raises(AudioTooLarge, match="2000 bytes"):
            await service.transcribe(url_source(size_bytes=2000))
        assert client.calls == 0, "size guard fires before the provider is called"
        # failure reason carries numbers only — no paths, no text
        assert "stored.wav" not in caplog.text

    async def test_1kb_limit_mirror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Manual scenario 6 mirror: AUDIO_MAX_BYTES=1000 → failed, size reason
        monkeypatch.setattr(settings, "audio_max_bytes", 1000)
        client = RecorderClient()
        service = TranscriptionService(client)
        with pytest.raises(AudioTooLarge):
            await service.transcribe(url_source(size_bytes=1001))
        assert client.calls == 0

    async def test_at_limit_is_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "audio_max_bytes", 1000)
        client = RecorderClient(outcomes=[one_segment()])
        service = TranscriptionService(client)
        transcript = await service.transcribe(url_source(size_bytes=1000))
        assert client.calls == 1
        assert len(transcript.segments) == 1


class TestRetryPolicy:
    async def test_transient_exhausts_three_attempts_with_backoff(self, tmp_path: Any) -> None:
        client = RecorderClient()
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        service = TranscriptionService(client, sleeper=sleeper)
        with pytest.raises(PermanentTranscriptionError, match="3 attempts"):
            await service.transcribe(url_source())
        assert client.calls == RETRY_ATTEMPTS
        # webhooks precedent (workers/webhook_dispatcher.py:130): sleeps run
        # only BETWEEN attempts, so 3 attempts sleep [1, 4]; 16 s is the
        # sequence's third step, not a post-final wait.
        assert sleeps == [1.0, 4.0]

    async def test_transient_recovers_on_third_attempt(self, tmp_path: Any) -> None:
        ok = Transcript(segments=[SpeakerSegment(speaker="S0", start=0.0, end=1.0, text="hello")])
        client = RecorderClient(
            outcomes=[
                TransientTranscriptionError("429"),
                TransientTranscriptionError("503"),
                ok,
            ]
        )
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        service = TranscriptionService(client, sleeper=sleeper)
        transcript = await service.transcribe(url_source())
        assert client.calls == 3
        assert sleeps == [1.0, 4.0]
        assert transcript.segments[0].text == "hello"

    async def test_permanent_fails_immediately_without_retries(self, tmp_path: Any) -> None:
        client = RecorderClient(permanent=True)
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        service = TranscriptionService(client, sleeper=sleeper)
        with pytest.raises(PermanentTranscriptionError, match="401"):
            await service.transcribe(url_source())
        assert client.calls == 1
        assert sleeps == []

    async def test_permanent_message_is_call_id_safe(self, tmp_path: Any) -> None:
        # the raised detail goes into failure_reason — it must never carry
        # transcript content, local paths, or presigned-URL fragments
        client = RecorderClient(permanent=True)
        service = TranscriptionService(client)
        with pytest.raises(PermanentTranscriptionError) as exc_info:
            await service.transcribe(url_source())
        message = str(exc_info.value)
        assert "stored.wav" not in message
        assert "sig=1" not in message
        assert "dg-" not in message

    async def test_source_passed_through_unmodified(self, tmp_path: Any) -> None:
        client = RecorderClient(outcomes=[one_segment()])
        service = TranscriptionService(client)
        source = url_source(size_bytes=2048)
        await service.transcribe(source)
        assert client.sources == [source]


class TestStubProvider:
    async def test_stub_returns_honest_empty_transcript(self) -> None:
        client = StubTranscriptionClient(log_warnings=False)
        transcript = await client.transcribe(url_source(size_bytes=12345))
        assert transcript.segments == []

    async def test_stub_warns_once_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        client = StubTranscriptionClient()
        with caplog.at_level(logging.WARNING):
            client.warn_once()
        assert "stub" in caplog.text
        assert "EMPTY transcript" in caplog.text

    async def test_stub_supports_realtime_is_false(self) -> None:
        assert StubTranscriptionClient.supports_realtime is False


class TestLogHygieneMirrors:
    async def test_transcript_text_never_in_logs(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        secret_text = "zz-secret-words-alpha"
        client = RecorderClient(outcomes=[one_segment(secret_text)])
        service = TranscriptionService(client)
        transcript = await service.transcribe(local_source(tmp_path, size_bytes=3200))
        assert transcript.segments[0].text == secret_text  # text flows correctly
        assert secret_text not in caplog.text, "transcript text must never be logged"
        assert "segments=1" in caplog.text, "only counts are logged"

    async def test_local_audio_path_never_in_logs(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client = RecorderClient(outcomes=[one_segment()])
        service = TranscriptionService(client)
        source = AudioSource(local_path=tmp_path / "audio.wav", size_bytes=3200)
        await service.transcribe(source)
        assert str(tmp_path / "audio.wav") not in caplog.text
        assert "audio.wav" not in caplog.text
