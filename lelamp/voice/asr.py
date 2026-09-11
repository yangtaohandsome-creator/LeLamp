import asyncio
import json
import os
import time
import wave
from pathlib import Path
import numpy as np
import websockets
from .config import SAMPLE_RATE, env_float, voice_debug
from .audio import write_mono_wav

async def transcribe(audio: np.ndarray) -> str:
    asr_url = os.getenv("ASR_WS_URL", "ws://192.168.40.209:6006")
    unpadded_pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    padding_seconds = env_float("ASR_TAIL_SILENCE_SECONDS", 0.5)
    if not 0 <= padding_seconds <= 5:
        raise ValueError("ASR_TAIL_SILENCE_SECONDS 必须在 0～5 秒之间")
    pcm = unpadded_pcm + bytes(round(padding_seconds * SAMPLE_RATE) * 2)
    debug_path = None
    if voice_debug():
        try:
            directory = Path(os.getenv("VOICE_DEBUG_DIR", "voice_debug"))
            directory.mkdir(parents=True, exist_ok=True)
            debug_path = directory / str(time.time_ns())
            capture_path = debug_path.with_name(debug_path.name + "_capture.wav")
            asr_path = debug_path.with_name(debug_path.name + "_asr_input.wav")
            write_mono_wav(capture_path, audio)
            with wave.open(str(asr_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            print(f"原始录音: {capture_path.resolve()}", flush=True)
            print(f"ASR 输入录音: {asr_path.resolve()}", flush=True)
        except OSError as exc:
            print(f"诊断录音保存失败: {exc}", flush=True)
            debug_path = None

    def log_reply(reply):
        if voice_debug():
            line = json.dumps({"time": time.time(), "response": reply}, ensure_ascii=False)
            print(f"ASR RAW: {line}", flush=True)
            if debug_path is not None:
                try:
                    with debug_path.with_suffix(".jsonl").open("a") as log:
                        log.write(line + "\n")
                except OSError as exc:
                    print(f"诊断日志保存失败: {exc}", flush=True)

    final_text = ""
    async with websockets.connect(asr_url, max_size=2**20) as ws:
        for offset in range(0, len(pcm), 3200):  # 100 ms PCM chunks
            await ws.send(pcm[offset : offset + 3200])
        await ws.send('{"action":"eof"}')
        while True:
            reply = json.loads(await ws.recv())
            log_reply(reply)
            if reply.get("text"):
                final_text = reply["text"].strip()
            if reply.get("is_final"):
                if voice_debug():
                    # Observe later segments without changing which text is returned.
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        try:
                            extra = await asyncio.wait_for(ws.recv(), timeout=min(1.0, deadline - time.monotonic()))
                        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                            break
                        log_reply(json.loads(extra))
                return final_text
