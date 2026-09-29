"""Independent acoustic echo cancellation diagnostic for LeLamp.

This module intentionally does not import or start LampApp, Agent, ASR, TTS
playback, lighting, or motion.  It drives one GStreamer duplex pipeline so the
PCM observed by WebRTC's echo probe is exactly the PCM sent to the speaker.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from ..voice.config import load_voice_config


RATE = 48_000
FRAME_MS = 100
DEFAULT_TEXT = (
    "小灯正在播放一段测试语音。这段声音用于测量扬声器回声，"
    "请按照终端提示，在播放期间自然说话。"
)
GUIDED_PHRASES = (
    "停",
    "等等",
    "我问的是昨天那个",
    "请告诉我今天发生了什么",
    "先别说了，我想问另外一个问题",
)


@dataclass(frozen=True)
class RunMetrics:
    channel: str
    reference_delay_ms: int
    suppression_level: str
    duration_seconds: float
    wall_seconds: float
    raw_rms: float
    clean_rms: float
    suppression_db: float
    clean_peak: float
    clean_vad_triggered: bool
    clean_vad_active_blocks: int
    average_cpu_percent: float
    peak_rss_mb: float
    realtime_overrun_seconds: float
    output_dir: str


def _gst_quote(value: str) -> str:
    """Quote one gst-launch string property without invoking a shell."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_pipeline(
    *,
    reference_wav: Path,
    raw_wav: Path,
    clean_wav: Path,
    channel: str,
    reference_delay_ms: int,
    suppression_level: str = "moderate",
    capture_device: str,
    playback_device: str,
) -> list[str]:
    if channel not in {"channel_0", "channel_1", "mean"}:
        raise ValueError("channel 必须是 channel_0、channel_1 或 mean")
    if not 0 <= reference_delay_ms <= 500:
        raise ValueError("reference delay 必须在 0～500 ms")
    if suppression_level not in {"low", "moderate", "high"}:
        raise ValueError("suppression level 必须是 low、moderate 或 high")

    render = (
        f"filesrc location={_gst_quote(str(reference_wav))} ! wavparse ! "
        "audioconvert ! audioresample ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels=1 ! tee name=render "
        f"render. ! queue ! alsasink device={_gst_quote(playback_device)} sync=true "
        "render. ! queue ! "
        f"identity ts-offset={reference_delay_ms * 1_000_000} ! "
        "webrtcechoprobe name=webrtcechoprobe0 ! fakesink sync=true "
    )
    capture_prefix = (
        f"alsasrc device={_gst_quote(capture_device)} do-timestamp=true ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels=2 ! "
    )
    if channel == "mean":
        select = (
            "audioconvert ! "
            f"audio/x-raw,format=S16LE,rate={RATE},channels=1 ! tee name=near "
        )
    else:
        pad = 0 if channel == "channel_0" else 1
        select = (
            "deinterleave name=mic_channels "
            f"mic_channels.src_{pad} ! queue ! "
            f"audio/x-raw,format=S16LE,rate={RATE},channels=1 ! tee name=near "
        )
    processing = (
        f"near. ! queue ! wavenc ! filesink location={_gst_quote(str(raw_wav))} "
        "near. ! queue ! webrtcdsp name=aec probe=webrtcechoprobe0 "
        "echo-cancel=true delay-agnostic=true extended-filter=true "
        f"echo-suppression-level={suppression_level} gain-control=false "
        "noise-suppression=false high-pass-filter=false ! "
        f"wavenc ! filesink location={_gst_quote(str(clean_wav))}"
    )
    # gst-launch parses its own pipeline language. Passing it as argv keeps the
    # command free from shell interpolation while retaining quoted properties.
    return ["gst-launch-1.0", "-q", "-e"] + shlex.split(
        render + capture_prefix + select + processing
    )


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"只支持 S16_LE WAV: {path}")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio.copy(), rate


def _write_wav(path: Path, audio: np.ndarray, rate: int = RATE) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or len(audio) == 0:
        return audio.copy()
    target_count = max(1, round(len(audio) * target_rate / source_rate))
    source_x = np.arange(len(audio), dtype=np.float64)
    target_x = np.linspace(0, len(audio) - 1, target_count, dtype=np.float64)
    return np.interp(target_x, source_x, audio).astype(np.float32)


async def _download_reference_mp3(path: Path, text: str) -> None:
    import edge_tts

    voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
    rate = os.getenv("EDGE_TTS_RATE", "+10%")
    await edge_tts.Communicate(text=text, voice=voice, rate=rate).save(str(path))


def prepare_reference(
    output: Path,
    *,
    duration_seconds: float,
    source_wav: Path | None,
    text: str,
    volume_percent: float,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if source_wav is None:
        with tempfile.TemporaryDirectory(prefix="lelamp-aec-") as temporary:
            mp3 = Path(temporary) / "reference.mp3"
            decoded = Path(temporary) / "reference.wav"
            asyncio.run(_download_reference_mp3(mp3, text))
            subprocess.run(
                ["mpg123", "-q", "-w", str(decoded), str(mp3)], check=True
            )
            source, source_rate = _read_wav(decoded)
    else:
        source, source_rate = _read_wav(source_wav)
    source = _resample(source, source_rate, RATE)
    source = np.clip(source * (volume_percent / 100.0), -1, 1)
    silence = np.zeros(round(0.35 * RATE), dtype=np.float32)
    unit = np.concatenate([source, silence])
    required = max(1, round(duration_seconds * RATE))
    repeated = np.tile(unit, math.ceil(required / max(1, len(unit))))[:required]
    _write_wav(output, repeated)
    return output


def _centered_rms(audio: np.ndarray) -> float:
    if len(audio) == 0:
        return 0.0
    centered = audio - float(audio.mean())
    return float(np.sqrt(np.mean(np.square(centered))))


def _vad_summary(clean: np.ndarray, rate: int) -> tuple[bool, int]:
    """Run the project's Silero VAD offline at 16 kHz."""
    from ..voice.vad import make_vad

    clean_16k = _resample(clean, rate, 16_000)
    detector = make_vad()
    if detector is None:
        threshold = float(os.getenv("VAD_START_RMS_THRESHOLD", "0.008"))
        blocks = [clean_16k[i:i + 1600] for i in range(0, len(clean_16k), 1600)]
        active = sum(_centered_rms(block) >= threshold for block in blocks if len(block))
        return active > 0, active
    active = 0
    for start in range(0, len(clean_16k), 1600):
        block = clean_16k[start:start + 1600]
        if len(block) < 1600:
            block = np.pad(block, (0, 1600 - len(block)))
        detector.accept_waveform(block)
        active += int(detector.is_speech_detected())
    return active > 0, active


def _write_block_trace(path: Path, raw: np.ndarray, clean: np.ndarray, rate: int) -> None:
    """Write and print aligned 100 ms RMS/VAD diagnostics."""
    from ..voice.vad import make_vad

    block_samples = round(rate * FRAME_MS / 1000)
    detector = make_vad()
    threshold = float(os.getenv("VAD_START_RMS_THRESHOLD", "0.008"))
    rows: list[dict[str, float | bool | int]] = []
    count = min(len(raw), len(clean))
    for index, start in enumerate(range(0, count, block_samples)):
        raw_block = raw[start:start + block_samples]
        clean_block = clean[start:start + block_samples]
        if not len(clean_block):
            continue
        raw_rms = _centered_rms(raw_block)
        clean_rms = _centered_rms(clean_block)
        suppression = 20 * math.log10(max(raw_rms, 1e-9) / max(clean_rms, 1e-9))
        clean_16k = _resample(clean_block, rate, 16_000)
        if detector is None:
            vad_active = clean_rms >= threshold
        else:
            detector.accept_waveform(clean_16k)
            vad_active = bool(detector.is_speech_detected())
        row = {
            "block": index,
            "elapsed_seconds": start / rate,
            "raw_rms": raw_rms,
            "clean_rms": clean_rms,
            "suppression_db": suppression,
            "vad_active": vad_active,
        }
        rows.append(row)
        print(
            f"AEC BLOCK {row['elapsed_seconds']:06.2f}s | raw={raw_rms:.5f} "
            f"clean={clean_rms:.5f} | suppress={suppression:6.2f}dB | "
            f"VAD={'SPEECH' if vad_active else 'quiet'}",
            flush=True,
        )
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "block", "elapsed_seconds", "raw_rms", "clean_rms",
                "suppression_db", "vad_active",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)


def _process_stats(pid: int, stop: threading.Event, output: dict[str, float]) -> None:
    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    previous_cpu = None
    previous_at = None
    cpu_samples: list[float] = []
    rss_samples: list[float] = []
    while not stop.wait(0.1):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            status = Path(f"/proc/{pid}/status").read_text().splitlines()
            cpu = (int(stat[13]) + int(stat[14])) / ticks
            now = time.monotonic()
            if previous_cpu is not None and now > previous_at:
                cpu_samples.append((cpu - previous_cpu) / (now - previous_at) * 100)
            previous_cpu, previous_at = cpu, now
            rss_line = next(line for line in status if line.startswith("VmRSS:"))
            rss_samples.append(float(rss_line.split()[1]) / 1024)
        except (FileNotFoundError, ProcessLookupError, StopIteration):
            break
    output["average_cpu_percent"] = float(np.mean(cpu_samples)) if cpu_samples else 0.0
    output["peak_rss_mb"] = max(rss_samples, default=0.0)


def run_once(
    *,
    root: Path,
    reference_wav: Path,
    duration_seconds: float,
    channel: str,
    reference_delay_ms: int,
    suppression_level: str,
    capture_device: str,
    playback_device: str,
    guided_phrases: tuple[str, ...] = (),
) -> RunMetrics:
    run_dir = root / f"{channel}_delay_{reference_delay_ms:03d}ms"
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_wav = run_dir / "raw_mic.wav"
    clean_wav = run_dir / "aec_clean.wav"
    shutil.copy2(reference_wav, run_dir / "reference.wav")
    command = build_pipeline(
        reference_wav=reference_wav,
        raw_wav=raw_wav,
        clean_wav=clean_wav,
        channel=channel,
        reference_delay_ms=reference_delay_ms,
        suppression_level=suppression_level,
        capture_device=capture_device,
        playback_device=playback_device,
    )
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    stats: dict[str, float] = {}
    stop_stats = threading.Event()
    monitor = threading.Thread(
        target=_process_stats, args=(process.pid, stop_stats, stats), daemon=True
    )
    monitor.start()
    guide_stop = threading.Event()

    def guide() -> None:
        # Give WebRTC AEC five seconds to converge, then leave six seconds for
        # each utterance. This keeps VAD hangover from joining adjacent tests.
        if guide_stop.wait(5.0):
            return
        for index, phrase in enumerate(guided_phrases, 1):
            print(f"\n>>> 第 {index}/{len(guided_phrases)} 句，现在请说：{phrase}", flush=True)
            if guide_stop.wait(6.0):
                return
        print("\n>>> 五句话已完成，请保持安静，等待程序保存结果。", flush=True)

    guide_thread = None
    if guided_phrases:
        guide_thread = threading.Thread(target=guide, daemon=True)
        guide_thread.start()
    try:
        time.sleep(duration_seconds + 0.25)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=8)
    except Exception:
        process.kill()
        _, stderr = process.communicate()
        raise
    finally:
        guide_stop.set()
        if guide_thread is not None:
            guide_thread.join(timeout=1)
        stop_stats.set()
        monitor.join(timeout=1)
    wall = time.monotonic() - started
    if process.returncode not in {0, -signal.SIGINT}:
        raise RuntimeError(f"GStreamer AEC 失败 ({process.returncode}): {stderr[-2000:]}")
    if not raw_wav.is_file() or not clean_wav.is_file():
        raise RuntimeError(f"GStreamer 未生成诊断 WAV: {stderr[-2000:]}")

    raw, raw_rate = _read_wav(raw_wav)
    clean, clean_rate = _read_wav(clean_wav)
    if raw_rate != clean_rate:
        clean = _resample(clean, clean_rate, raw_rate)
        clean_rate = raw_rate
    _write_block_trace(run_dir / "trace.csv", raw, clean, clean_rate)
    # Ignore startup/shutdown transients and compare equal-length samples.
    trim = round(0.5 * min(raw_rate, clean_rate))
    count = min(len(raw), len(clean))
    raw = raw[trim:max(trim, count - trim)]
    clean = clean[trim:max(trim, count - trim)]
    raw_rms = _centered_rms(raw)
    clean_rms = _centered_rms(clean)
    suppression = 20 * math.log10(max(raw_rms, 1e-9) / max(clean_rms, 1e-9))
    vad_triggered, active_blocks = _vad_summary(clean, clean_rate)
    metrics = RunMetrics(
        channel=channel,
        reference_delay_ms=reference_delay_ms,
        suppression_level=suppression_level,
        duration_seconds=duration_seconds,
        wall_seconds=wall,
        raw_rms=raw_rms,
        clean_rms=clean_rms,
        suppression_db=suppression,
        clean_peak=float(np.max(np.abs(clean))) if len(clean) else 0.0,
        clean_vad_triggered=vad_triggered,
        clean_vad_active_blocks=active_blocks,
        average_cpu_percent=stats.get("average_cpu_percent", 0.0),
        peak_rss_mb=stats.get("peak_rss_mb", 0.0),
        realtime_overrun_seconds=max(0.0, wall - duration_seconds - 0.25),
        output_dir=str(run_dir),
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2) + "\n"
    )
    return metrics


def _print_metrics(metrics: RunMetrics) -> None:
    print(
        f"AEC {metrics.channel} delay={metrics.reference_delay_ms:3d}ms | "
        f"level={metrics.suppression_level} | "
        f"raw={metrics.raw_rms:.5f} clean={metrics.clean_rms:.5f} | "
        f"抑制={metrics.suppression_db:.2f}dB | "
        f"VAD={'TRIGGER' if metrics.clean_vad_triggered else 'quiet'} "
        f"({metrics.clean_vad_active_blocks}) | "
        f"CPU={metrics.average_cpu_percent:.1f}% RSS={metrics.peak_rss_mb:.1f}MB | "
        f"超时={metrics.realtime_overrun_seconds:.3f}s",
        flush=True,
    )


def _check_dependencies() -> None:
    required = ("gst-launch-1.0", "gst-inspect-1.0", "mpg123")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise RuntimeError("缺少命令: " + ", ".join(missing))
    for plugin in ("webrtcdsp", "webrtcechoprobe", "alsasrc", "alsasink"):
        result = subprocess.run(
            ["gst-inspect-1.0", plugin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode:
            raise RuntimeError(f"GStreamer 插件不可用: {plugin}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LeLamp 独立 WebRTC AEC 诊断")
    parser.add_argument("--mode", choices=("scan", "interactive"), default="scan")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--interactive-duration", type=float, default=45.0)
    parser.add_argument("--channel", choices=("channel_0", "channel_1", "mean"))
    parser.add_argument("--delay-ms", type=int)
    parser.add_argument("--delay-step-ms", type=int, default=10)
    parser.add_argument("--max-delay-ms", type=int, default=200)
    parser.add_argument(
        "--suppression-level", choices=("low", "moderate", "high"), default="moderate"
    )
    parser.add_argument("--reference-wav", type=Path)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--volume-percent", type=float, default=110.0)
    parser.add_argument(
        "--guided", action="store_true",
        help="每隔 6 秒在终端提示一条真人插话测试语句",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("voice_debug/aec"))
    args = parser.parse_args()
    if args.duration <= 1 or args.interactive_duration <= 1:
        raise ValueError("测试时长必须大于 1 秒")
    if args.delay_step_ms <= 0 or args.max_delay_ms < 0:
        raise ValueError("delay 扫描参数无效")

    load_voice_config()
    _check_dependencies()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = args.output_dir / stamp
    root.mkdir(parents=True, exist_ok=True)
    duration = args.interactive_duration if args.mode == "interactive" else args.duration
    if args.guided and args.mode != "interactive":
        raise ValueError("--guided 只能与 --mode interactive 一起使用")
    if args.guided and duration < 38:
        raise ValueError("guided 真人测试时长至少需要 38 秒")
    reference = prepare_reference(
        root / "reference_fixture.wav",
        duration_seconds=duration,
        source_wav=args.reference_wav,
        text=args.text,
        volume_percent=args.volume_percent,
    )
    capture_device = os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0")
    playback_device = os.getenv("APLAY_DEVICE", "plughw:seeed2micvoicec,0")

    if args.mode == "interactive":
        if args.channel is None or args.delay_ms is None:
            raise ValueError("interactive 模式必须传 --channel 和 --delay-ms")
        print("AEC INTERACTIVE READY", flush=True)
        print("播放期间请依次说：停、等等、我问的是昨天那个，以及两句自然问题。", flush=True)
        result = run_once(
            root=root, reference_wav=reference, duration_seconds=duration,
            channel=args.channel, reference_delay_ms=args.delay_ms,
            suppression_level=args.suppression_level,
            capture_device=capture_device, playback_device=playback_device,
            guided_phrases=GUIDED_PHRASES if args.guided else (),
        )
        _print_metrics(result)
        summary = {"mode": "interactive", "result": asdict(result)}
    else:
        channels = [args.channel] if args.channel else ["channel_0", "channel_1", "mean"]
        delays = [args.delay_ms] if args.delay_ms is not None else list(
            range(0, args.max_delay_ms + 1, args.delay_step_ms)
        )
        print("AEC ECHO-ONLY SCAN READY", flush=True)
        print("扫描期间请保持安静；程序会重复播放测试语音。", flush=True)
        results: list[RunMetrics] = []
        for channel in channels:
            for delay in delays:
                result = run_once(
                    root=root, reference_wav=reference, duration_seconds=duration,
                    channel=channel, reference_delay_ms=delay,
                    suppression_level=args.suppression_level,
                    capture_device=capture_device, playback_device=playback_device,
                )
                results.append(result)
                _print_metrics(result)
        ranked = sorted(
            results,
            key=lambda item: (
                item.clean_vad_triggered,
                -item.suppression_db,
                item.average_cpu_percent,
            ),
        )
        best = ranked[0]
        print(
            f"BEST: {best.channel}, delay={best.reference_delay_ms}ms, "
            f"抑制={best.suppression_db:.2f}dB, "
            f"VAD={'triggered' if best.clean_vad_triggered else 'quiet'}",
            flush=True,
        )
        summary = {
            "mode": "scan",
            "pass_thresholds": {
                "minimum_suppression_db": 12.0,
                "allow_clean_vad_trigger": False,
                "maximum_average_cpu_percent": 100.0,
                "maximum_peak_rss_mb": 100.0,
            },
            "best": asdict(best),
            "results": [asdict(item) for item in results],
        }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"AEC 诊断结果已保存: {root.resolve()}", flush=True)


if __name__ == "__main__":
    main()
