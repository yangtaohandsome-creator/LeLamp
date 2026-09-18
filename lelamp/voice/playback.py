"""Cancellation and PCM taps shared by TTS backends."""
from __future__ import annotations

import threading
from collections.abc import Callable


class SpeechInterrupted(Exception):
    """Expected cancellation caused by a user barge-in."""


class PlaybackControl:
    def __init__(
        self,
        *,
        on_format: Callable[[int, int], None] | None = None,
        on_pcm: Callable[[bytes, int, int], None] | None = None,
    ) -> None:
        self._cancelled = threading.Event()
        self.on_format = on_format
        self.on_pcm = on_pcm

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def format(self, sample_rate: int, channels: int) -> None:
        if self.on_format is not None:
            self.on_format(sample_rate, channels)

    def pcm(self, data: bytes, sample_rate: int, channels: int) -> None:
        if self.on_pcm is not None and not self.cancelled:
            self.on_pcm(data, sample_rate, channels)
