"""Wake-word voice assistant for LeLamp.

Pipeline: local KWS -> local silence detection -> ASR WebSocket -> LLM -> TTS -> speaker.
All credentials are read from environment variables.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import sherpa_onnx
import websockets
from dotenv import load_dotenv

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 1600  # 100 ms


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def make_spotter(model_dir: Path, keywords_file: Path) -> sherpa_onnx.KeywordSpotter:
    return sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / "tokens.txt"),
        encoder=str(model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=str(model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        joiner=str(model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        keywords_file=str(keywords_file),
        num_threads=int(os.getenv("KWS_NUM_THREADS", "1")),
        provider="cpu",
        keywords_score=1.0,
        keywords_threshold=env_float("KWS_THRESHOLD", 0.25),
    )


def start_capture() -> subprocess.Popen:
    """Use ALSA directly: PortAudio does not expose this ReSpeaker's input."""
    return subprocess.Popen(
        [
            "arecord",
            "-D", os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"),
            "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", "48000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def read_capture_block(capture: subprocess.Popen) -> np.ndarray:
    """Read 100 ms of 48 kHz stereo PCM and convert it to 16 kHz mono."""
    assert capture.stdout is not None
    raw = capture.stdout.read(48000 // 10 * 2 * 2)
    if len(raw) != 48000 // 10 * 2 * 2:
        raise RuntimeError("arecord stopped unexpectedly")
    stereo = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32)
    return (stereo.mean(axis=1)[::3] / 32768.0).copy()


def stop_capture(capture: subprocess.Popen) -> None:
    capture.terminate()
    try:
        capture.wait(timeout=3)
    except subprocess.TimeoutExpired:
        capture.kill()
        capture.wait()
    if capture.stdout is not None:
        capture.stdout.close()


def capture_utterance(read_block) -> np.ndarray | None:
    """Read speech after KWS, ending after sustained silence."""
    silence_limit = int(env_float("VAD_SILENCE_SECONDS", 1.0) * SAMPLE_RATE)
    start_timeout = env_float("VAD_START_TIMEOUT_SECONDS", 5.0)
    max_samples = int(env_float("VAD_MAX_SECONDS", 15.0) * SAMPLE_RATE)
    threshold = env_float("VAD_RMS_THRESHOLD", 0.008)
    chunks: list[np.ndarray] = []
    heard_speech = False
    silent_samples = 0
    started_at = time.monotonic()

    print("WAKE: lao_deng; listening...", flush=True)
    while sum(len(x) for x in chunks) < max_samples:
        block = read_block()
        rms = float(np.sqrt(np.mean(np.square(block))))
        if rms >= threshold:
            heard_speech = True
            silent_samples = 0
        elif heard_speech:
            silent_samples += len(block)
        elif time.monotonic() - started_at > start_timeout:
            print("No speech after wake word.", flush=True)
            return None

        if heard_speech:
            chunks.append(block)
        if heard_speech and silent_samples >= silence_limit:
            break

    if not chunks:
        return None
    return np.concatenate(chunks)


async def transcribe(audio: np.ndarray) -> str:
    asr_url = os.getenv("ASR_WS_URL", "ws://192.168.40.209:6006")
    pcm = np.clip(audio, -1, 1)
    pcm = (pcm * 32767).astype("<i2").tobytes()
    final_text = ""
    async with websockets.connect(asr_url, max_size=2**20) as ws:
        for offset in range(0, len(pcm), 3200):  # 100 ms PCM chunks
            await ws.send(pcm[offset : offset + 3200])
        await ws.send('{"action":"eof"}')
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("text"):
                final_text = reply["text"].strip()
            if reply.get("is_final"):
                return final_text


def ask_llm(text: str) -> str:
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    api_key = os.environ["OPENAI_API_KEY"]
    model = os.getenv("LLM_MODEL", "qwen3.7-flash")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是老灯，一盏友好、简洁的交互台灯。用中文回答，最多三句话。",
            },
            {"role": "user", "content": text},
        ],
        "stream": False,
    }
    response = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def speak(text: str) -> tuple[float, float]:
    tts_url = os.getenv("TTS_URL", "http://192.168.40.209:8200/v1/tts/stream")
    tts_started = time.perf_counter()
    response = httpx.post(tts_url, json={"text": text}, timeout=90)
    response.raise_for_status()
    tts_seconds = time.perf_counter() - tts_started
    with wave.open(io.BytesIO(response.content), "rb") as wav:
        if wav.getcomptype() != "NONE":
            raise ValueError("TTS must return an uncompressed WAV")
    playback_started = time.perf_counter()
    subprocess.run(
        ["aplay", "-q", "-D",
         os.getenv("APLAY_DEVICE", "plughw:seeed2micvoicec,0"), "-t", "wav"],
        input=response.content, check=True, timeout=120,
    )
    playback_seconds = time.perf_counter() - playback_started
    return tts_seconds, playback_seconds


def main() -> None:
    load_dotenv()
    model_dir = Path(os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"))
    keywords_file = Path(os.getenv("KWS_KEYWORDS_FILE", str(model_dir / "keywords_lao_deng.txt")))
    spotter = make_spotter(model_dir, keywords_file)
    stream = spotter.create_stream()
    print("Voice assistant ready. Say: 老灯", flush=True)
    capture = start_capture()
    try:
        while True:
            samples = read_capture_block(capture)
            stream.accept_waveform(SAMPLE_RATE, samples)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
            if not spotter.get_result(stream):
                continue
            spotter.reset_stream(stream)
            turn_started = time.perf_counter()
            listen_started = time.perf_counter()
            utterance = capture_utterance(lambda: read_capture_block(capture))
            listen_seconds = time.perf_counter() - listen_started
            print(f"LISTEN END | 延迟: {listen_seconds:.2f} 秒", flush=True)
            if utterance is None:
                continue
            # Release the capture clock before playback and discard buffered audio.
            stop_capture(capture)
            capture = None
            try:
                asr_started = time.perf_counter()
                text = asyncio.run(asyncio.wait_for(transcribe(utterance), timeout=45))
                asr_seconds = time.perf_counter() - asr_started
                print(f"ASR: {text}", flush=True)
                print(f"ASR 延迟: {asr_seconds:.2f} 秒", flush=True)
                if text:
                    llm_started = time.perf_counter()
                    answer = ask_llm(text)
                    llm_seconds = time.perf_counter() - llm_started
                    print(f"LLM: {answer}", flush=True)
                    print(f"LLM 延迟: {llm_seconds:.2f} 秒", flush=True)
                    tts_seconds, playback_seconds = speak(answer)
                    print(f"TTS 延迟: {tts_seconds:.2f} 秒", flush=True)
                    print(f"播放耗时: {playback_seconds:.2f} 秒", flush=True)
                    print(
                        f"本轮总耗时: {time.perf_counter() - turn_started:.2f} 秒",
                        flush=True,
                    )
            except Exception as exc:
                print(f"Voice turn failed: {exc}", file=sys.stderr, flush=True)
            capture = start_capture()
            stream = spotter.create_stream()
            print("Ready. Say: 老灯", flush=True)
    finally:
        if capture is not None:
            stop_capture(capture)


if __name__ == "__main__":
    main()
