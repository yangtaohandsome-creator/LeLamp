#!/usr/bin/python3
"""Standalone dynamic barge-in experiment; intentionally independent of LampApp."""
from __future__ import annotations

import argparse
import array
import math
import signal
import shutil
import subprocess
import threading
import time
import wave
from collections import deque
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

RATE = 48_000


def conf_float(name: str, fallback: float) -> float:
    path = Path(__file__).resolve().parents[2] / "voice.conf"
    try:
        for line in path.read_text().splitlines():
            if line.startswith(name + "="):
                return float(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return fallback


def rms_pcm(data: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        return 0.0
    mean = sum(samples) / len(samples)
    return math.sqrt(sum((x - mean) ** 2 for x in samples) / len(samples)) / 32768.0


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DynamicBargeIn:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.detected_at = None
        self.last_voice_at = None
        self.clean = bytearray()
        self.pre_roll = deque(maxlen=round(args.pre_roll * RATE * 2 / 960) + 2)
        self.utterance = bytearray()
        self.hot = 0
        self.silero_speech = threading.Event()
        self.vad_process = None
        self.loop = GLib.MainLoop()
        channel_pad = 0 if args.channel == "channel_0" else 1
        pipeline = (
            f"filesrc location={quote(str(args.reference.resolve()))} ! wavparse ! "
            f"audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate={RATE},channels=1 ! tee name=render "
            f"render. ! queue ! valve name=speaker_valve ! alsasink name=speaker device={quote(args.playback_device)} sync=true "
            f"render. ! queue ! valve name=reference_valve ! identity ts-offset={args.delay_ms * 1000000} ! "
            "webrtcechoprobe name=probe ! fakesink sync=true "
            f"alsasrc device={quote(args.capture_device)} do-timestamp=true ! "
            f"audio/x-raw,format=S16LE,rate={RATE},channels=2 ! deinterleave name=mic "
            f"mic.src_{channel_pad} ! queue ! audio/x-raw,format=S16LE,rate={RATE},channels=1 ! "
            "tee name=near near. ! queue ! appsink name=raw emit-signals=true sync=false max-buffers=8 drop=true "
            "near. ! queue ! webrtcdsp probe=probe echo-cancel=true delay-agnostic=true extended-filter=true "
            f"echo-suppression-level={args.suppression_level} gain-control=false "
            "noise-suppression=false high-pass-filter=false ! "
            "appsink name=clean emit-signals=true sync=false max-buffers=8 drop=true"
        )
        self.pipeline = Gst.parse_launch(pipeline)
        self.valve = self.pipeline.get_by_name("speaker_valve")
        self.reference_valve = self.pipeline.get_by_name("reference_valve")
        self.speaker = self.pipeline.get_by_name("speaker")
        sink = self.pipeline.get_by_name("clean")
        sink.connect("new-sample", self.on_sample)
        raw_sink = self.pipeline.get_by_name("raw")
        raw_sink.connect("new-sample", self.on_raw_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

    def start_vad(self):
        if self.args.detector != "silero":
            return
        uv = shutil.which("uv") or "/home/lamppi/.local/bin/uv"
        root = Path(__file__).resolve().parents[2]
        self.vad_process = subprocess.Popen(
            [
                uv, "run", "--no-sync", "-m", "lelamp.test.vad_stream",
                "--threshold", str(self.args.vad_threshold),
                "--min-speech", str(self.args.vad_min_speech),
            ],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, bufsize=0,
        )

        def watch():
            for line in self.vad_process.stdout:
                message = line.decode(errors="replace").strip()
                if message == "SPEECH":
                    self.silero_speech.set()
        threading.Thread(target=watch, daemon=True).start()

    def stop_speaker(self):
        # This callback runs on a GStreamer streaming thread. Dropping new
        # buffers is safe here; changing sink state is deferred to the GLib
        # thread to avoid deadlocking the pipeline. Sending flush events
        # directly caused the sink to lose its segment and stall capture.
        self.valve.set_property("drop", True)
        self.reference_valve.set_property("drop", True)
        GLib.idle_add(self._stop_speaker_sink)
        print("INTERRUPT DETECTED | TTS cancelled | listening continues", flush=True)

    def _stop_speaker_sink(self):
        self.speaker.set_state(Gst.State.NULL)
        return False

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        data = bytes(mapped.data)
        buffer.unmap(mapped)
        now = time.monotonic()
        level = rms_pcm(data)
        if self.detected_at is None:
            self.pre_roll.append(data)
            if self.vad_process is not None and self.vad_process.stdin is not None:
                try:
                    self.vad_process.stdin.write(data)
                except (BrokenPipeError, OSError):
                    pass
            if now - self.started >= self.args.warmup:
                if self.args.detector == "rms":
                    self.hot = self.hot + 1 if level >= self.args.threshold else 0
                    triggered = self.hot >= self.args.start_blocks
                else:
                    triggered = self.silero_speech.is_set()
                if triggered:
                    self.detected_at = now
                    self.last_voice_at = now
                    self.utterance.extend(b"".join(self.pre_roll))
                    self.stop_speaker()
        else:
            # After cancellation, the raw callback owns capture. Continuing to
            # use AEC output can suppress near speech while its adaptive state
            # settles after the far-end reference disappears.
            pass
        if now - self.started >= self.args.timeout:
            GLib.idle_add(self.loop.quit)
        return Gst.FlowReturn.OK

    def on_raw_sample(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        data = bytes(mapped.data)
        buffer.unmap(mapped)
        if self.detected_at is None:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        self.utterance.extend(data)
        if rms_pcm(data) >= self.args.continue_threshold:
            self.last_voice_at = now
        elif now - self.last_voice_at >= self.args.silence:
            GLib.idle_add(self.loop.quit)
        return Gst.FlowReturn.OK

    def on_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"GStreamer ERROR: {err}: {debug}", flush=True)
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            self.loop.quit()

    def run(self):
        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(self.loop.quit))
        signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(self.loop.quit))
        self.start_vad()
        self.started = time.monotonic()
        self.pipeline.set_state(Gst.State.PLAYING)
        print("DYNAMIC BARGE-IN READY", flush=True)
        print(f"请等待 {self.args.warmup:.0f} 秒后，在小灯播报期间自然说一句完整的话。", flush=True)
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            if self.vad_process is not None:
                if self.vad_process.stdin is not None:
                    self.vad_process.stdin.close()
                self.vad_process.terminate()
                try:
                    self.vad_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.vad_process.kill()
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
        if self.detected_at is None:
            print("RESULT: 未检测到真人开口", flush=True)
            return 2
        self.args.output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.args.output), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
            wav.writeframes(self.utterance)
        latency = (self.detected_at - self.started) * 1000
        print(f"RESULT: 已保存 {self.args.output} | 检测时刻={latency:.0f}ms | 时长={len(self.utterance)/2/RATE:.2f}s", flush=True)
        if not self.args.no_asr:
            uv = shutil.which("uv") or "/home/lamppi/.local/bin/uv"
            root = Path(__file__).resolve().parents[2]
            try:
                completed = subprocess.run(
                    [uv, "run", "--no-sync", "-m", "lelamp.test.transcribe_wav", str(self.args.output.resolve())],
                    cwd=root, text=True, timeout=45, check=True,
                )
                if completed.returncode == 0:
                    print("DYNAMIC BARGE-IN TEST COMPLETE", flush=True)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"ASR TEST FAILED: {exc}", flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("voice_debug/aec/dynamic_utterance.wav"))
    parser.add_argument("--channel", choices=("channel_0", "channel_1"), default="channel_0")
    parser.add_argument("--delay-ms", type=int, default=80)
    parser.add_argument("--suppression-level", choices=("low", "moderate", "high"), default="low")
    parser.add_argument("--capture-device", default="hw:seeed2micvoicec,0")
    parser.add_argument("--playback-device", default="plughw:seeed2micvoicec,0")
    parser.add_argument("--threshold", type=float, default=.008)
    parser.add_argument("--detector", choices=("silero", "rms"), default="silero")
    parser.add_argument(
        "--vad-threshold", type=float,
        default=conf_float("BARGE_IN_VAD_THRESHOLD", .25),
    )
    parser.add_argument(
        "--vad-min-speech", type=float,
        default=conf_float("BARGE_IN_VAD_MIN_SPEECH_SECONDS", .05),
    )
    parser.add_argument("--continue-threshold", type=float, default=.004)
    parser.add_argument("--start-blocks", type=int, default=2)
    parser.add_argument("--pre-roll", type=float, default=.5)
    parser.add_argument("--silence", type=float, default=1.2)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--no-asr", action="store_true")
    args = parser.parse_args()
    Gst.init(None)
    raise SystemExit(DynamicBargeIn(args).run())


if __name__ == "__main__":
    main()
