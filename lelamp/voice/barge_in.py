"""Lightweight duplex speech interruption using WebRTC AEC plus VAD/KWS."""
from __future__ import annotations

import os
import importlib.util
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .config import SAMPLE_RATE, env_float
from .kws import make_spotter
from .vad import make_vad


STOP_PHRASES = ("别说了", "闭嘴", "等等", "行了", "停")


@dataclass(frozen=True)
class BargeInUtterance:
    audio: np.ndarray
    trigger: str
    keyword: str | None = None


def strip_stop_prefix(text: str) -> tuple[str, bool]:
    """Remove one or more explicit control prefixes from an ASR sentence."""
    value = text.strip()
    removed = False
    while value:
        candidate = value.lstrip("，,。！？!?、 ")
        match = next((word for word in STOP_PHRASES if candidate.startswith(word)), None)
        if match is None:
            break
        removed = True
        value = candidate[len(match):]
    return value.lstrip("，,。！？!?、 "), removed


class BargeInUnavailable(RuntimeError):
    pass


class BargeInSession:
    """One TTS duplex window. Business decisions remain in LampApp."""

    RATE = 48_000
    BLOCK_BYTES = RATE // 10 * 2

    def __init__(
        self,
        *,
        spotter,
        motion_active: Callable[[], bool],
        cancel_playback: Callable[[], None],
    ) -> None:
        self._spotter = spotter
        self._motion_active = motion_active
        self._cancel_playback = cancel_playback
        self._pipeline = None
        self._appsrc = None
        self._gst = None
        self._worker: threading.Thread | None = None
        self._blocks: queue.Queue[tuple[str, bytes] | None] = queue.Queue(maxsize=40)
        self._reference_blocks: queue.Queue[bytes | None] = queue.Queue(maxsize=80)
        self._reference_worker: threading.Thread | None = None
        self._closed = threading.Event()
        self._triggered = threading.Event()
        self._result_ready = threading.Event()
        self._result: BargeInUtterance | None = None
        self._trigger = ""
        self._keyword: str | None = None
        # Before cancellation only AEC-clean audio is safe to retain.  Raw mic
        # contains the lamp's own speech and would be transcribed as the user's
        # interruption.  After playback stops we switch to raw mic so WebRTC
        # cannot suppress the rest of the near-end sentence.
        self._clean_pre_roll: deque[bytes] = deque(
            maxlen=max(1, round(env_float("BARGE_IN_PRE_ROLL_SECONDS", 0.5) * 10))
        )
        self._utterance = bytearray()
        self._last_voice_at = 0.0
        self._last_motion_at = 0.0
        self._started = False
        self._aec_confirmed = False
        self._format: tuple[int, int] | None = None

    def start(self, sample_rate: int, channels: int) -> None:
        if self._started:
            return
        self._started = True
        self._format = (sample_rate, channels)
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as exc:
            raise BargeInUnavailable(f"PyGObject/GStreamer 不可用: {exc}") from exc
        Gst.init(None)
        self._gst = Gst
        capture_device = os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0")
        channel = int(os.getenv("BARGE_IN_AEC_CHANNEL", "0"))
        delay_ns = int(env_float("BARGE_IN_AEC_DELAY_MS", 80)) * 1_000_000
        level = os.getenv("BARGE_IN_AEC_LEVEL", "low").strip().lower()
        pipeline = (
            f"appsrc name=reference is-live=true format=time do-timestamp=true "
            f"caps=audio/x-raw,format=S16LE,layout=interleaved,rate={sample_rate},channels={channels} ! "
            "audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            f"identity ts-offset={delay_ns} ! webrtcechoprobe name=probe ! fakesink sync=true "
            f"alsasrc device={capture_device} do-timestamp=true ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2 ! deinterleave name=mic "
            f"mic.src_{channel} ! queue ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            "tee name=near "
            "near. ! queue ! appsink name=raw emit-signals=true sync=false max-buffers=8 drop=true "
            "near. ! queue ! webrtcdsp probe=probe echo-cancel=true delay-agnostic=true "
            f"extended-filter=true echo-suppression-level={level} gain-control=false "
            "noise-suppression=false high-pass-filter=false ! "
            "appsink name=clean emit-signals=true sync=false max-buffers=8 drop=true"
        )
        self._pipeline = Gst.parse_launch(pipeline)
        self._appsrc = self._pipeline.get_by_name("reference")
        self._pipeline.get_by_name("raw").connect("new-sample", self._on_raw)
        self._pipeline.get_by_name("clean").connect("new-sample", self._on_clean)
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise BargeInUnavailable("GStreamer AEC 启动失败")
        self._worker = threading.Thread(
            target=self._detect_loop, name="lelamp-barge-in", daemon=True
        )
        self._worker.start()
        self._reference_worker = threading.Thread(
            target=self._reference_loop, name="lelamp-aec-reference", daemon=True
        )
        self._reference_worker.start()
        print("AEC starting", flush=True)

    def _pop_pipeline_error(self) -> str | None:
        if self._pipeline is None or self._gst is None:
            return None
        bus = self._pipeline.get_bus()
        message = bus.timed_pop_filtered(0, self._gst.MessageType.ERROR)
        if message is None:
            return None
        error, debug = message.parse_error()
        return f"{error.message} ({debug or 'no debug'})"

    def feed_pcm(self, pcm: bytes, sample_rate: int, channels: int) -> None:
        if self._closed.is_set():
            return
        if not self._started:
            self.start(sample_rate, channels)
        if self._format != (sample_rate, channels) or self._appsrc is None:
            return
        # Decoders can produce PCM faster than the ALSA device consumes it.
        # Timestamping every chunk immediately would make the AEC reference run
        # ahead of the sound that actually leaves the speaker.  A dedicated
        # worker therefore feeds the exact PCM at its real audio duration.
        try:
            self._reference_blocks.put(pcm, timeout=0.5)
        except queue.Full:
            # Losing reference is safer than stalling TTS indefinitely.  The
            # session remains usable and simply becomes less sensitive.
            if os.getenv("BARGE_IN_DEBUG", "0") == "1":
                print("BARGE DEBUG | reference queue full", flush=True)

    def _reference_loop(self) -> None:
        assert self._format is not None
        sample_rate, channels = self._format
        bytes_per_second = sample_rate * channels * 2
        deadline = time.monotonic()
        while not self._closed.is_set() and not self._triggered.is_set():
            try:
                pcm = self._reference_blocks.get(timeout=0.1)
            except queue.Empty:
                deadline = time.monotonic()
                continue
            if pcm is None:
                return
            wait = deadline - time.monotonic()
            if wait > 0 and self._closed.wait(wait):
                return
            if self._triggered.is_set() or self._appsrc is None:
                return
            Gst = self._gst
            buffer = Gst.Buffer.new_allocate(None, len(pcm), None)
            buffer.fill(0, pcm)
            self._appsrc.emit("push-buffer", buffer)
            error = self._pop_pipeline_error()
            if error:
                print(f"AEC pipeline error: {error}", flush=True)
                return
            if not self._aec_confirmed:
                _change, state, _pending = self._pipeline.get_state(
                    int(0.5 * Gst.SECOND)
                )
                error = self._pop_pipeline_error()
                if error:
                    print(f"AEC pipeline error: {error}", flush=True)
                    return
                if state == Gst.State.PLAYING:
                    self._aec_confirmed = True
                    print("AEC active", flush=True)
            deadline = max(deadline, time.monotonic()) + len(pcm) / bytes_per_second

    def _pull(self, sink, kind: str):
        Gst = self._gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        data = bytes(mapped.data)
        buffer.unmap(mapped)
        try:
            self._blocks.put_nowait((kind, data))
        except queue.Full:
            pass
        return Gst.FlowReturn.OK

    def _on_raw(self, sink):
        return self._pull(sink, "raw")

    def _on_clean(self, sink):
        return self._pull(sink, "clean")

    @staticmethod
    def _to_16k(data: bytes) -> np.ndarray:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        return samples[::3].copy()

    @staticmethod
    def _rms(data: bytes) -> float:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
        if not len(samples):
            return 0.0
        samples -= samples.mean()
        return float(np.sqrt(np.mean(np.square(samples))) / 32768.0)

    def _detect_loop(self) -> None:
        def new_vad():
            return make_vad(
                threshold=env_float("BARGE_IN_IDLE_VAD_THRESHOLD", 0.35),
                min_speech_seconds=env_float(
                    "BARGE_IN_IDLE_MIN_SPEECH_SECONDS", 0.15
                ),
            )

        vad = new_vad()
        spotter = self._spotter
        stream = spotter.create_stream()
        raw_buffer = bytearray()
        clean_buffer = bytearray()
        silence_seconds = env_float("VAD_SILENCE_SECONDS", 1.2)
        continue_rms = env_float("VAD_CONTINUE_RMS_THRESHOLD", 0.008)
        release_seconds = env_float("BARGE_IN_MOTION_RELEASE_MS", 300) / 1000.0
        max_seconds = env_float("VAD_MAX_SECONDS", 15.0)
        triggered_at = 0.0
        previous_moving: bool | None = None
        debug = os.getenv("BARGE_IN_DEBUG", "0") == "1"
        last_debug_at = 0.0
        while not self._closed.is_set():
            try:
                item = self._blocks.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                return
            kind, data = item
            target = raw_buffer if kind == "raw" else clean_buffer
            target.extend(data)
            while len(target) >= self.BLOCK_BYTES:
                block = bytes(target[:self.BLOCK_BYTES])
                del target[:self.BLOCK_BYTES]
                now = time.monotonic()
                if kind == "raw":
                    if self._triggered.is_set():
                        self._utterance.extend(block)
                        if self._rms(block) >= continue_rms:
                            self._last_voice_at = now
                        elif now - self._last_voice_at >= silence_seconds:
                            self._finish_result()
                            return
                        if now - triggered_at >= max_seconds:
                            self._finish_result()
                            return
                    continue
                if self._triggered.is_set():
                    continue
                self._clean_pre_roll.append(block)
                moving = bool(self._motion_active())
                if previous_moving is not None and moving != previous_moving:
                    if moving:
                        stream = spotter.create_stream()
                    else:
                        vad = new_vad()
                previous_moving = moving
                pcm16 = self._to_16k(block)
                # Explicit stop words remain a fast path in both modes.  This
                # lets the generic near-end VAD require a little more evidence
                # without making short commands such as “停” feel sluggish.
                stream.accept_waveform(SAMPLE_RATE, pcm16)
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                keyword = str(spotter.get_result(stream) or "").strip()
                if keyword:
                    spotter.reset_stream(stream)
                    self._trigger = "keyword"
                    self._keyword = keyword
                    triggered_at = now
                    self._begin_interrupt(now)
                    continue
                if moving:
                    self._last_motion_at = now
                elif now - self._last_motion_at >= release_seconds and vad is not None:
                    vad.accept_waveform(pcm16)
                    if debug and now - last_debug_at >= 0.5:
                        last_debug_at = now
                        print(
                            f"BARGE DEBUG | mode=vad | clean_rms={self._rms(block):.5f} "
                            f"| speech={vad.is_speech_detected()}",
                            flush=True,
                        )
                    if vad.is_speech_detected():
                        self._trigger = "speech"
                        triggered_at = now
                        self._begin_interrupt(now)

    def _begin_interrupt(self, now: float) -> None:
        if self._triggered.is_set():
            return
        self._triggered.set()
        try:
            self._reference_blocks.put_nowait(None)
        except queue.Full:
            pass
        self._utterance.extend(b"".join(self._clean_pre_roll))
        self._last_voice_at = now
        print(
            "user speech detected" if self._trigger == "speech"
            else f"interrupt keyword detected: {self._keyword}",
            flush=True,
        )
        self._cancel_playback()

    def _finish_result(self) -> None:
        if self._result_ready.is_set():
            return
        audio48 = np.frombuffer(bytes(self._utterance), dtype="<i2").astype(np.float32)
        audio = (audio48[::3] / 32768.0).copy()
        self._result = BargeInUtterance(audio, self._trigger, self._keyword)
        self._result_ready.set()

    def wait_result(self, timeout: float | None = None) -> BargeInUtterance | None:
        if not self._triggered.is_set():
            return None
        self._result_ready.wait(timeout)
        return self._result

    @property
    def interrupted(self) -> bool:
        return self._triggered.is_set()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._blocks.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._reference_blocks.put_nowait(None)
        except queue.Full:
            pass
        if self._appsrc is not None:
            try:
                self._appsrc.emit("end-of-stream")
            except Exception:
                pass
        if self._pipeline is not None and self._gst is not None:
            self._pipeline.set_state(self._gst.State.NULL)
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        if (
            self._reference_worker is not None
            and self._reference_worker is not threading.current_thread()
        ):
            self._reference_worker.join(timeout=2)


class BargeInController:
    def __init__(self) -> None:
        model_dir = Path(os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"))
        self.model_dir = model_dir
        self.keywords_file = Path(os.getenv(
            "BARGE_IN_MOTION_KEYWORDS_FILE", str(model_dir / "keywords_interrupt.txt")
        ))
        self._spotter = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return (
            os.getenv("BARGE_IN_ENABLED", "0") == "1"
            and importlib.util.find_spec("gi") is not None
        )

    def create_session(self, *, motion_active, cancel_playback) -> BargeInSession | None:
        if not self.enabled:
            return None
        if not self.keywords_file.is_file():
            print(f"Barge-in 停止词文件不存在，回退播完再听: {self.keywords_file}", flush=True)
            return None
        try:
            self.prepare()
        except Exception as exc:
            print(f"Barge-in KWS 不可用，回退播完再听: {exc}", flush=True)
            return None
        return BargeInSession(
            spotter=self._spotter,
            motion_active=motion_active,
            cancel_playback=cancel_playback,
        )

    def prepare(self) -> None:
        if not self.enabled or self._spotter is not None:
            return
        with self._lock:
            if self._spotter is None:
                self._spotter = make_spotter(
                    self.model_dir,
                    self.keywords_file,
                    score=env_float("BARGE_IN_MOTION_KWS_SCORE", 0.5),
                    threshold=env_float("BARGE_IN_MOTION_KWS_THRESHOLD", 0.15),
                )
                print("Barge-in KWS ready", flush=True)
