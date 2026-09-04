"""Capture the virtual speaker monitor into a WAV file via `parec`.

The format is pinned by the shared contracts and must never change:
16-bit PCM, 16 kHz, mono — directly acceptable by Deepgram/AssemblyAI,
so nothing downstream ever transcodes.

Runs alongside the Playwright join flow: `start()` spawns `parec` and a
small thread that drains its stderr (so the pipe can never fill and
block), `stop()` terminates it cleanly so the WAV header is finalized.
Audio bytes are never read into memory or logged.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger("oreeai.bot.record")

_START_GRACE_S = 1.0
_STOP_TIMEOUT_S = 5.0
_STDERR_TAIL_LINES = 20

PAREC_ARGS: tuple[str, ...] = (
    "--monitor-source=virtual_speaker.monitor",
    "--rate=16000",
    "--channels=1",
    "--format=s16le",
    "--file-format=wav",
)


class Recorder:
    def __init__(self, wav_path: str) -> None:
        self._wav_path = wav_path
        self._proc: subprocess.Popen[str] | None = None
        self._drain: threading.Thread | None = None
        self._stderr_tail: list[str] = []

    @property
    def wav_path(self) -> str:
        return self._wav_path

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._wav_path) or ".", exist_ok=True)
        self._stderr_tail = []
        self._proc = subprocess.Popen(
            ("parec", *PAREC_ARGS, self._wav_path),
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
