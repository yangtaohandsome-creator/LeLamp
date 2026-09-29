"""Continuous listening over ALSA dsnoop, shared with per-turn AEC clients."""
from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from typing import Callable

import numpy as np


DEFAULT_DEVICE = "dsnoop:CARD=seeed2micvoicec,DEV=0"


class SharedCapture:
    """Keep ALSA's underlying capture alive while consumers come and go."""

    BLOCK_BYTES = 48_000 // 10 * 2 * 4

    def __init__(self, observer: Callable[[np.ndarray], None] | None = None) -> None:
        self.device = os.getenv("VOICE_SHARED_CAPTURE_DEVICE", DEFAULT_DEVICE)
        self._observer = observer
        self._condition = threading.Condition()
        self._blocks: deque[np.ndarray] = deque(maxlen=10)
        self._listening = False
        self._closed = False
        self._error: BaseException | None = None
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            [
                "arecord", "-q", "-D", self.device, "-t", "raw",
                "-f", "S32_LE", "-c", "2", "-r", "48000",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._thread = threading.Thread(
            target=self._read_loop, name="lelamp-microphone", daemon=True
        )
        self._thread.start()

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                raw = self._process.stdout.read(self.BLOCK_BYTES)
                if len(raw) != self.BLOCK_BYTES:
                    detail = ""
                    if self._process.stderr is not None:
                        detail = self._process.stderr.read().decode(errors="replace").strip()
                    raise RuntimeError(detail or "arecord stopped unexpectedly")
                stereo = np.frombuffer(raw, dtype="<i4").reshape(-1, 2)
                samples = (stereo[::3].astype(np.float32) / 2147483648.0).copy()
                if self._observer is not None:
                    self._observer(samples)
                with self._condition:
                    if self._listening:
                        self._blocks.append(samples)
                        self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                if not self._closed:
                    self._error = exc
                self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            self._listening = False
            self._blocks.clear()

    def resume(self) -> None:
        with self._condition:
            self._blocks.clear()
            self._listening = True
            self._condition.notify_all()

    def read_stereo(self) -> np.ndarray:
        with self._condition:
            while not self._blocks and self._error is None and not self._closed:
                self._condition.wait(timeout=0.25)
            if self._blocks:
                return self._blocks.popleft()
            if self._error is not None:
                raise RuntimeError("持续采音已停止") from self._error
            raise RuntimeError("持续采音已关闭")

    def read_mono(self) -> np.ndarray:
        return self.read_stereo().mean(axis=1)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._blocks.clear()
            self._condition.notify_all()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            if self._process.stdout is not None:
                self._process.stdout.close()
            if self._process.stderr is not None:
                self._process.stderr.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
