"""Capture the virtual speaker monitor into a WAV file via `parec`.

The format is pinned by the shared contracts and must never change:
16-bit PCM, 16 kHz, mono — directly acceptable by Deepgram/AssemblyAI,
so nothing downstream ever transcodes.

Runs alongside the Playwright join flow: `start()` spawns `parec` and a
small thread that drains its stderr (so the pipe can never fill and
block), `stop()` terminates it cleanly so the WAV header is finalized.
The post-recording silence check reads samples in chunks; audio samples and
filesystem paths are never logged.
"""

from __future__ import annotations

import array
import logging
import math
import os
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("oreeai.bot.record")

_START_GRACE_S = 1.0
_STOP_TIMEOUT_S = 5.0
_STDERR_TAIL_LINES = 20

# parec has no --monitor-source option: recording from a sink records its monitor.
PAREC_DEVICE_DEFAULT = "virtual_speaker.monitor"
PAREC_ARGS: tuple[str, ...] = (
    f"--device={PAREC_DEVICE_DEFAULT}",
    "--rate=16000",
    "--channels=1",
    "--format=s16le",
    "--file-format=wav",
)

RecordingStatus = Literal["ok", "silent", "missing", "unreadable"]
_CHUNK_FRAMES = 16384


@dataclass(frozen=True)
class RecordingCheck:
    status: RecordingStatus
    rms: float | None
    detail: str


def _parec_device() -> str:
    """Return the PulseAudio device parec should record.

    The default is the operational monitor. ``PAREC_DEVICE`` is a diagnostic
    override only; it exists so a silent-source capture can be exercised
    without changing the shipped audio graph.
    """
    return os.environ.get("PAREC_DEVICE", "").strip() or PAREC_DEVICE_DEFAULT


def wav_rms(wav_path: str) -> float | None:
    """Return the whole-file RMS level of a 16-bit PCM WAV file.

    Returns ``None`` when the file is missing, unreadable, compressed, or not
    16-bit PCM. The value uses raw sample units (0-32767 for signed 16-bit
    audio), matching ``BOT_SILENCE_RMS_FLOOR``. Audio samples are processed in
    chunks and never logged.
    """
    try:
        with wave.open(wav_path, "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                return None
            if wav.getnframes() <= 0:
                return 0.0

            squared_total = 0.0
            sample_total = 0
            while True:
                raw = wav.readframes(_CHUNK_FRAMES)
                if not raw:
                    break
                samples = array.array("h", raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                if len(samples) == 0:
                    break
                squared_total += math.dist(samples, bytes(len(samples))) ** 2
                sample_total += len(samples)

            if sample_total == 0:
                return 0.0
            return math.sqrt(squared_total / sample_total)
    except (OSError, ValueError, OverflowError, wave.Error, EOFError):
        return None


def check_recording(wav_path: str, silence_rms_floor: float) -> RecordingCheck:
    """Classify a finished recording without exposing its path or samples."""
    rms = wav_rms(wav_path)
    if rms is None:
        if not os.path.exists(wav_path):
            return RecordingCheck("missing", None, "recording file missing")
        return RecordingCheck(
            "unreadable", None, "recording file unreadable or has unsupported format"
        )
    if rms < silence_rms_floor:
        return RecordingCheck("silent", rms, "recording below silence floor")
    return RecordingCheck("ok", rms, "recording has audio signal")


class Recorder:
    def __init__(self, wav_path: str) -> None:
        self._wav_path = wav_path
        self._proc: subprocess.Popen[str] | None = None
        self._drain: threading.Thread | None = None
        self._stderr_tail: list[str] = []

    @property
    def wav_path(self) -> str:
        return self._wav_path

    def is_running(self) -> bool:
        """True while the parec child is alive (unexpected death ⇒ exit 5)."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._wav_path) or ".", exist_ok=True)
        self._stderr_tail = []
        self._proc = subprocess.Popen(
            (
                "parec",
                f"--device={_parec_device()}",
                *(arg for arg in PAREC_ARGS if not arg.startswith("--device=")),
                self._wav_path,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()
        time.sleep(_START_GRACE_S)
        if self._proc.poll() is not None:
            tail = "".join(self._stderr_tail).strip()
            self._proc = None
            raise RuntimeError(f"parec exited immediately: {tail or 'no stderr output'}")
        logger.info("parec running (pid %s)", self._proc.pid)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > _STDERR_TAIL_LINES:
                del self._stderr_tail[0]

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.warning("parec did not stop on SIGTERM; killing (WAV may be truncated)")
                proc.kill()
                proc.wait(timeout=_STOP_TIMEOUT_S)
        if proc.returncode not in (0, None):
            logger.warning("parec exited with code %s", proc.returncode)
        if self._drain is not None:
            self._drain.join(timeout=2.0)
        try:
            size = os.path.getsize(self._wav_path)
        except OSError:
            logger.error("recording file missing after stop")
            return
        logger.info("recording finished: %s bytes", size)
