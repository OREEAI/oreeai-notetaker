"""Unit tests for post-recording silence classification."""

import math
import struct
import wave
from collections.abc import Iterable
from pathlib import Path

import pytest
from bot.record_audio import check_recording, wav_rms

SAMPLE_RATE = 16000
SILENCE_FLOOR = 50.0


def write_pcm16(path: Path, samples: Iterable[int]) -> Path:
    frames = b"".join(struct.pack("<h", sample) for sample in samples)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)
    return path


def sine_samples(amplitude: int, seconds: float = 1.0) -> list[int]:
    count = int(SAMPLE_RATE * seconds)
    return [
        int(amplitude * math.sin(2.0 * math.pi * 440.0 * sample / SAMPLE_RATE))
        for sample in range(count)
    ]


def test_silent_recording_is_below_floor(tmp_path: Path) -> None:
    path = write_pcm16(tmp_path / "silent.wav", [0] * SAMPLE_RATE)

    assert wav_rms(str(path)) == 0.0
    assert check_recording(str(path), SILENCE_FLOOR).status == "silent"


def test_soft_but_real_signal_is_above_floor(tmp_path: Path) -> None:
    # A short, soft recording must not be mistaken for a dead audio graph.
    path = write_pcm16(tmp_path / "soft.wav", sine_samples(100, seconds=1.0))
    rms = wav_rms(str(path))

    assert rms == pytest.approx(100 / math.sqrt(2), rel=0.01)
    assert check_recording(str(path), SILENCE_FLOOR).status == "ok"


def test_floor_boundary_passes(tmp_path: Path) -> None:
    path = write_pcm16(tmp_path / "boundary.wav", [50] * 1000)

    assert check_recording(str(path), SILENCE_FLOOR).status == "ok"


def test_missing_and_unsupported_recordings_are_distinct(tmp_path: Path) -> None:
    missing = tmp_path / "missing.wav"
    unsupported = tmp_path / "eight-bit.wav"
    with wave.open(str(unsupported), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes([128] * 1000))

    assert wav_rms(str(missing)) is None
    assert check_recording(str(missing), SILENCE_FLOOR).status == "missing"
    assert wav_rms(str(unsupported)) is None
    assert check_recording(str(unsupported), SILENCE_FLOOR).status == "unreadable"
