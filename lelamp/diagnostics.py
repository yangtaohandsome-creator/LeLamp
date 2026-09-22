"""Read-only LeLamp health checks. No sound, motion, or service restart."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
from urllib.parse import urlparse

import httpx
import numpy as np

from .voice.config import load_voice_config

ROOT = Path(__file__).resolve().parents[1]
MOTIONS = ("happy_wiggle", "excited", "sad", "shy", "shock", "nod", "headshake", "curious")


class AudioHealthMonitor:
    """Recent capture facts, updated by the existing arecord reader thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks: deque[tuple[float, float]] = deque(maxlen=30)
        self._updated = 0.0

    def observe(self, stereo: np.ndarray) -> None:
        centered = stereo - stereo.mean(axis=0)
        rms = np.sqrt(np.mean(np.square(centered), axis=0))
        with self._lock:
            self._blocks.append((float(rms[0]), float(rms[1])))
            self._updated = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            values = list(self._blocks)
            age = time.monotonic() - self._updated if self._updated else None
        return {
            "age_seconds": round(age, 2) if age is not None else None,
            "blocks": len(values),
            "rms": [round(float(np.median([row[i] for row in values])), 6) for i in range(2)] if values else [],
            "peak_rms": [round(max(row[i] for row in values), 6) for i in range(2)] if values else [],
        }


def _check(name: str, status: str, detail: str, suggestion: str = "") -> dict:
    return {"id": name, "status": status, "detail": detail, "suggestion": suggestion}


def _report(checks: list[dict], state: dict | None = None) -> dict:
    statuses = {item["status"] for item in checks}
    overall = "failed" if "failed" in statuses else "warning" if "warning" in statuses else "ok"
    return {
        "overall": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "robot_state": state,
    }


def _tcp_probe(url: str, timeout: float = 0.5) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def _files_and_config() -> list[dict]:
    checks = []
    model_dir = ROOT / os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
    keywords = ROOT / os.getenv("KWS_KEYWORDS_FILE", str(model_dir / "keywords_xiao_deng.txt"))
    model_files = ("tokens.txt", "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx")
    missing = [p for p in model_files if not (model_dir / p).is_file()]
    if not keywords.is_file():
        missing.append(keywords.name)
    checks.append(_check("kws_files", "failed" if missing else "ok", "缺少: " + ", ".join(missing) if missing else "模型和小灯词表齐全", "检查 KWS_MODEL_DIR / KWS_KEYWORDS_FILE" if missing else ""))
    channel = os.getenv("KWS_AUDIO_CHANNEL", "mean").strip().lower()
    checks.append(_check("kws_channel", "ok" if channel in {"mean", "0", "1", "channel_0", "channel_1"} else "failed", f"当前通道: {channel}", "设为 0、1 或 mean" if channel not in {"mean", "0", "1", "channel_0", "channel_1"} else ""))
    port = Path(os.getenv("MOTION_PORT", "/dev/ttyACM0"))
    port_ready = port.exists() and os.access(port, os.R_OK | os.W_OK)
    checks.append(_check("servo_port", "ok" if port_ready else "failed", f"{port} {'可访问' if port_ready else '不存在或无读写权限'}", "检查 USB 串口连接和 lamppi 用户权限" if not port_ready else ""))
    missing_actions = [name for name in MOTIONS if not (ROOT / "lelamp" / "recordings" / f"{name}.csv").is_file()]
    checks.append(_check("recordings", "failed" if missing_actions else "ok", "缺少用户动作: " + ", ".join(missing_actions) if missing_actions else "八份用户动作文件齐全", "从备份恢复动作 CSV" if missing_actions else ""))
    try:
        from .motion.config import sleep_action, standby_action, reading_action, reading_low_action
        for pose in (sleep_action, standby_action, reading_action, reading_low_action):
            pose()
        checks.append(_check("motion_poses", "ok", "睡眠、待机、高低照明姿态配置齐全"))
    except (KeyError, ValueError) as exc:
        checks.append(_check("motion_poses", "failed", f"姿态配置不完整: {exc}", "检查 motion.conf"))
    hf_home = Path(os.getenv("HF_HOME", "~/.cache/huggingface")).expanduser()
    lerobot_home = Path(os.getenv("HF_LEROBOT_HOME", str(hf_home / "lerobot"))).expanduser()
    calibration_root = Path(os.getenv("HF_LEROBOT_CALIBRATION", str(lerobot_home / "calibration"))).expanduser()
    calibration = calibration_root / "robots" / "lelamp_follower" / f"{os.getenv('MOTION_LAMP_ID', 'lamppi')}.json"
    try:
        data = json.loads(calibration.read_text())
        required = {"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"}
        valid = isinstance(data, dict) and required.issubset(data)
        checks.append(_check("calibration", "ok" if valid else "failed", "五个关节的校准文件存在；未比对舵机寄存器" if valid else "校准文件缺少关节", "检查 LeRobot 校准文件" if not valid else ""))
    except (OSError, ValueError) as exc:
        checks.append(_check("calibration", "failed", f"校准文件不可读取或无效: {type(exc).__name__}", "检查 LeRobot 校准文件；不会自动重校准"))
    asr = os.getenv("ASR_WS_URL", "")
    asr_ready = bool(asr and _tcp_probe(asr))
    checks.append(_check("asr", "ok" if asr_ready else "failed", "ASR 端口可连接（尚未验证识别质量）" if asr_ready else "ASR 地址未配置或端口不可连接", "检查 ASR_WS_URL 和服务状态" if not asr_ready else ""))
    backend = os.getenv("TTS_BACKEND", "remote").lower()
    if backend == "edge":
        ready = shutil.which("mpg123") is not None and shutil.which("aplay") is not None
        checks.append(_check("tts", "ok" if ready else "failed", "Edge TTS 已配置，播放依赖齐全；未合成或播放" if ready else "缺少 mpg123 或 aplay", "安装播放依赖" if not ready else ""))
    elif backend == "remote":
        url = os.getenv("TTS_URL", "")
        ready = bool(url and _tcp_probe(url))
        checks.append(_check("tts", "ok" if ready else "failed", "远程 TTS 端口可连接；未合成或播放" if ready else "远程 TTS 地址未配置或不可连接", "检查 TTS_URL 和服务"))
    else:
        checks.append(_check("tts", "failed", f"不支持的 TTS_BACKEND: {backend}", "检查 voice.conf"))
    return checks


async def _agent_check() -> dict:
    backend = os.getenv("AGENT_BACKEND", "pi").lower()
    if backend == "pi":
        url = os.getenv("PI_AGENT_URL", "http://127.0.0.1:18792").rstrip("/") + "/health"
        token = os.getenv("PI_AGENT_TOKEN", "")
    else:
        url = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/") + "/health"
        token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        return _check("agent", "failed", f"{backend} Agent token 未配置", "检查 Pi5 私有 .env")
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return _check("agent", "ok", f"{backend} Agent HTTP 健康检查通过；未执行模型推理")
    except httpx.HTTPError as exc:
        return _check("agent", "failed", f"{backend} Agent 不可用: {type(exc).__name__}", "启动或检查 Agent 进程与 token")


def _audio_check(snapshot: dict, *, capturing: bool) -> dict:
    if not capturing:
        return _check("microphone", "unknown", "当前播报或切换采音中，麦克风暂不可测", "播报结束后重试")
    age, rms = snapshot["age_seconds"], snapshot["rms"]
    if age is None or age > 10:
        return _check("microphone", "failed", "采音数据超过 10 秒未更新，语音循环可能卡住", "检查 arecord 和声卡占用")
    if age > 3:
        return _check("microphone", "warning", f"最近 {age:.1f} 秒未读取麦克风，可能正在处理对话", "回到等待唤醒后重试")
    if snapshot["blocks"] < 5:
        return _check("microphone", "unknown", "采音刚开始，样本不足", "一秒后重试")
    channel = os.getenv("KWS_AUDIO_CHANNEL", "mean").lower()
    selected = sum(rms) / 2 if channel == "mean" else rms[0 if channel in {"0", "channel_0"} else 1]
    if selected < 0.0001:
        return _check("microphone", "failed", f"所选通道近乎全零；双通道 RMS={rms}", "检查 ReSpeaker 接线、ALSA 采音开关和增益")
    if min(rms) < 0.0001:
        return _check("microphone", "warning", f"一个通道近乎全零；双通道 RMS={rms}", "检查双麦克风及所选 KWS 通道")
    item = _check("microphone", "ok", f"最近 {snapshot['blocks']} 块持续有输入；双通道 RMS={rms}；不能据此证明唤醒词识别率")
    item["metrics"] = snapshot
    return item


async def run_live(app) -> dict:
    checks = await asyncio.to_thread(_files_and_config)
    checks.insert(0, _check("app", "ok", "Control API 与主程序正在运行"))
    task = getattr(app, "voice_task", None)
    voice_ready = task is not None and not task.done()
    checks.append(_check("voice_loop", "ok" if voice_ready else "failed", "语音循环运行中" if voice_ready else "语音循环未运行", "检查 app 终端报错" if not voice_ready else ""))
    checks.append(_audio_check(app.audio_health.snapshot(), capturing=bool(getattr(app, "capture_active", False))))
    checks.append(await _agent_check())
    checks.append(_check("timer", "ok" if not app.timers._closed else "failed", "Timer 管理器可用" if not app.timers._closed else "Timer 管理器已关闭"))
    checks.append(_check("alarm", "ok" if app.alarms._started and not app.alarms._closed else "failed", "Alarm 调度已启动" if app.alarms._started and not app.alarms._closed else "Alarm 调度未运行"))
    worker = app.announcements._worker
    checks.append(_check("announcement", "ok" if worker is not None and not worker.done() else "failed", "播报队列消费者运行中" if worker is not None and not worker.done() else "播报队列消费者已停止"))
    vision = app.get_vision_state()
    vision_status = vision["status"]
    if not vision["enabled"]:
        checks.append(_check("vision", "unknown", "视觉功能已在配置中关闭"))
    elif vision_status == "error":
        checks.append(_check(
            "vision", "warning", f"视觉初始化或运行失败: {vision['error']}",
            "检查摄像头、模型路径和视觉依赖",
        ))
    elif vision["requested"]:
        checks.append(_check(
            "vision", "ok" if vision["running"] else "warning",
            f"视觉状态: {vision_status}；完成频率: {vision['inference_hz']} Hz",
            "稍后重试或检查摄像头" if not vision["running"] else "",
        ))
    else:
        checks.append(_check("vision", "ok", "当前模式无需视觉，摄像头已释放"))
    light_status = "failed" if app.lighting._unavailable else "ok" if app.lighting._rgb is not None else "unknown"
    checks.append(_check("lighting_driver", light_status, "灯光驱动初始化失败" if light_status == "failed" else "灯光驱动已初始化；实际发光需人工确认" if light_status == "ok" else "灯光驱动尚未初始化", "检查驱动和灯板接线" if light_status == "failed" else ""))
    checks.append(_check("physical_output", "unknown", "未实测扬声器、灯板亮度、实际关节角度或扭矩", "需要人工观察；自检不会播放或移动"))
    return _report(checks, app.get_robot_state())


def _offline_microphone() -> dict:
    import subprocess
    try:
        result = subprocess.run(
            ["arecord", "-D", os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"),
             "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", "48000", "-d", "1"],
            capture_output=True, timeout=3, check=False,
        )
        if result.returncode:
            return _check("microphone", "failed", "声卡采音失败或被其他进程占用", "检查接线、ALSA 和 arecord 占用")
        samples = np.frombuffer(result.stdout, dtype="<i2")
        if len(samples) < 96000:
            return _check("microphone", "failed", "录音不足一秒", "检查 ReSpeaker 输入设备")
        monitor = AudioHealthMonitor()
        for block in samples[:96000].reshape(-1, 2).reshape(10, 4800, 2):
            monitor.observe(block[::3].astype(np.float32) / 32768.0)
        return _audio_check(monitor.snapshot(), capturing=True)
    except Exception as exc:
        return _check("microphone", "failed", f"无法采音: {type(exc).__name__}: {exc}", "检查声卡连接及是否被其他进程占用")


async def run_offline(*, app_reachable: bool = False) -> dict:
    checks = await asyncio.to_thread(_files_and_config)
    checks.insert(0, _check("app", "failed", "Control API 可达但自检鉴权失败" if app_reachable else "LeLamp Control API 不可达；app 未运行", "检查 LELAMP_CONTROL_TOKEN" if app_reachable else "检查 lelamp.app 进程"))
    checks.append(await _agent_check())
    checks.append(_check("microphone", "unknown", "app 可能持有麦克风，未独立打开采音设备", "修复 Control API 鉴权后重试") if app_reachable else await asyncio.to_thread(_offline_microphone))
    for name in ("voice_loop", "timer", "alarm", "announcement", "lighting_driver"):
        checks.append(_check(name, "unknown", "app 未运行，无法读取当前状态"))
    checks.append(_check("physical_output", "unknown", "未实测灯板、扬声器、关节或扭矩"))
    return _report(checks)


async def _deep_check(kind: str) -> dict:
    try:
        if kind == "agent":
            key = os.getenv("OPENAI_API_KEY", "")
            if not key:
                return _check("model_inference", "failed", "模型 API Key 未配置", "检查 Pi5 私有 .env")
            base = os.getenv("LLM_BASE_URL", "").rstrip("/")
            url = base + ("" if base.endswith("/v1") else "/v1") + "/chat/completions"
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json={
                    "model": os.getenv("LLM_MODEL", "deepseek-flash"),
                    "messages": [{"role": "user", "content": "只回答：好"}],
                    "max_tokens": 8, "stream": False, "enable_thinking": False,
                })
            response.raise_for_status()
            if not response.json().get("choices"):
                raise ValueError("模型未返回结果")
            return _check("model_inference", "ok", "模型可返回文字；未调用 Agent 工具")
        if kind == "asr":
            import websockets
            async with websockets.connect(os.getenv("ASR_WS_URL", ""), open_timeout=3) as ws:
                await ws.send(bytes(32000))
                await ws.send('{"action":"eof"}')
                reply = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if reply.get("status") != "ok":
                raise ValueError("ASR 返回非 ok 状态")
            return _check("asr_request", "ok", "ASR 静音请求成功；仍需真人录音验证识别质量")
        if kind == "tts":
            backend = os.getenv("TTS_BACKEND", "edge").lower()
            if backend == "edge":
                import edge_tts
                communicator = edge_tts.Communicate("自检。", os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural"))
                async for item in communicator.stream():
                    if item.get("type") == "audio" and item.get("data"):
                        return _check("tts_request", "ok", "Edge TTS 已生成音频数据；未播放")
            else:
                async with httpx.AsyncClient(timeout=12) as client:
                    async with client.stream("POST", os.getenv("TTS_URL", ""), json={"text": "自检。", "format": "pcm"}) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                return _check("tts_request", "ok", "远程 TTS 已生成音频数据；未播放")
            raise ValueError("TTS 未返回音频")
    except Exception as exc:
        return _check(f"{kind}_request" if kind != "agent" else "model_inference", "failed", f"请求失败: {type(exc).__name__}: {exc}", "检查服务、网络与私有密钥")
    raise ValueError(kind)


async def run_cli() -> None:
    parser = argparse.ArgumentParser(description="LeLamp 一键只读自检")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--mic", action="store_true", help="引导说话，观察实时麦克风 RMS")
    parser.add_argument("--deep", choices=("agent", "asr", "tts", "all"), help="可选无声服务请求验证")
    args = parser.parse_args()
    load_voice_config()
    token = os.getenv("LELAMP_CONTROL_TOKEN", "")
    base = os.getenv("LELAMP_CONTROL_URL", "http://127.0.0.1:18790").rstrip("/")
    report = None
    if token:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(base + "/v1/tools/get_system_health", headers={"Authorization": f"Bearer {token}"}, json={})
            response.raise_for_status()
            report = response.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    if report is None:
        report = await run_offline(app_reachable=await asyncio.to_thread(_tcp_probe, base))
    if args.mic and report.get("robot_state") is not None:
        print("请对小灯说一句话，3 秒后再次检查麦克风状态。", file=sys.stderr)
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(base + "/v1/tools/get_system_health", headers={"Authorization": f"Bearer {token}"}, json={})
        response.raise_for_status()
        new = response.json()["data"]
        mic = next(c for c in new["checks"] if c["id"] == "microphone")
        if mic["status"] == "ok":
            stats = mic.get("metrics", {})
            rms = stats.get("rms", [])
            peak = stats.get("peak_rms", [])
            channel = os.getenv("KWS_AUDIO_CHANNEL", "mean").lower()
            index = 0 if channel in {"0", "channel_0"} else 1 if channel in {"1", "channel_1"} else None
            voice_peak = peak[index] if index is not None else sum(peak) / 2
            floor = rms[index] if index is not None else sum(rms) / 2
            if voice_peak < max(0.003, floor * 2):
                mic = _check("microphone", "warning", f"未观察到明显说话峰值；峰值={voice_peak:.5f}，背景={floor:.5f}", "确认说话时麦克风输入是否变化，检查接线与增益")
            else:
                mic["detail"] += f"；引导说话峰值={voice_peak:.5f}"
        report["checks"] = [item if item["id"] != "microphone" else mic for item in report["checks"]]
        report = _report(report["checks"], report["robot_state"])
    if args.deep:
        selected = ("agent", "asr", "tts") if args.deep == "all" else (args.deep,)
        report["checks"].extend(await asyncio.gather(*(_deep_check(kind) for kind in selected)))
        report = _report(report["checks"], report["robot_state"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        labels = {"ok": "正常", "warning": "警告", "failed": "异常", "unknown": "待人工确认"}
        print(f"小灯自检：{labels[report['overall']]}")
        for item in report["checks"]:
            print(f"[{labels[item['status']]}] {item['id']}: {item['detail']}")
            if item["suggestion"]:
                print(f"  建议：{item['suggestion']}")
    if report["overall"] == "failed":
        raise SystemExit(2)
    if report["overall"] == "warning":
        raise SystemExit(1)


def main() -> None:
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
