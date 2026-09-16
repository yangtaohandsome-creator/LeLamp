"""Preconnected Edge Neural TTS backend with streaming MP3 decoding."""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import ssl
import threading
import time
from typing import Callable

from .audio import scale_pcm_s16le
from .config import env_float


class EdgeTtsError(RuntimeError):
    pass


class EdgeTtsNotReady(EdgeTtsError):
    pass


class EdgeSpeechClient:
    """Own one short-lived warm WebSocket on a dedicated event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="edge-tts", daemon=True)
        self._started = threading.Event()
        self._closed = False
        self._session = None
        self._websocket = None
        self._connect_task: asyncio.Task | None = None
        self._thread.start()
        self._started.wait(timeout=2)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def preconnect(self) -> None:
        if self._closed:
            return
        future = asyncio.run_coroutine_threadsafe(self._prepare(), self._loop)

        def report(result: concurrent.futures.Future) -> None:
            try:
                ready = result.result()
                if ready:
                    print("Edge TTS 预连接已就绪", flush=True)
            except Exception as exc:
                print(f"Edge TTS 预连接失败，将回退远程 TTS: {exc}", flush=True)

        future.add_done_callback(report)

    def speak(
        self,
        text: str,
        on_playback_start: Callable[[], None] | None = None,
    ) -> tuple[float, float, float]:
        if self._closed:
            raise EdgeTtsError("Edge TTS 已关闭")
        future = asyncio.run_coroutine_threadsafe(
            self._speak(text, on_playback_start), self._loop
        )
        try:
            return future.result(timeout=120)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise EdgeTtsError("Edge TTS 超时") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        future = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    async def _prepare(self) -> bool:
        if self._closed or self._connection_ready():
            return self._connection_ready()
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(self._connect())
        await self._connect_task
        return self._connection_ready()

    def _connection_ready(self) -> bool:
        return self._websocket is not None and not self._websocket.closed

    async def _connect(self):
        import aiohttp
        import certifi
        from edge_tts.communicate import connect_id
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM

        await self._discard_connection()
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=env_float("EDGE_TTS_CONNECT_TIMEOUT_SECONDS", 3.0),
            sock_read=env_float("EDGE_TTS_RECEIVE_TIMEOUT_SECONDS", 30.0),
        )
        self._session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            self._websocket = await self._session.ws_connect(
                f"{WSS_URL}&ConnectionId={connect_id()}"
                f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
                f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
                compress=15,
                headers=DRM.headers_with_muid(WSS_HEADERS),
                ssl=ssl_context,
            )
            return self._websocket
        except Exception:
            await self._discard_connection()
            raise

    async def _take_ready_connection(self):
        if self._connection_ready():
            return self._websocket
        task = self._connect_task
        if task is None:
            # A missed speculative call must never turn into an unbounded delay.
            # Start preparing the next connection, then use the same short grace
            # period before allowing the configured remote fallback.
            task = self._connect_task = asyncio.create_task(self._connect())
        wait_seconds = env_float("EDGE_TTS_READY_WAIT_SECONDS", 0.15)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except Exception as exc:
            raise EdgeTtsNotReady("Edge TTS 预连接未及时完成") from exc
        if not self._connection_ready():
            raise EdgeTtsNotReady("Edge TTS 预连接不可用")
        return self._websocket

    async def _speak(self, text, on_playback_start):
        websocket = await self._take_ready_connection()
        retries = max(0, int(os.getenv("EDGE_TTS_RETRY_COUNT", "1")))
        try:
            for attempt in range(retries + 1):
                try:
                    return await self._synthesize(websocket, text, on_playback_start)
                except Exception:
                    await self._discard_connection()
                    if attempt >= retries:
                        raise
                    websocket = await asyncio.wait_for(
                        self._connect(),
                        timeout=env_float("EDGE_TTS_RETRY_TIMEOUT_SECONDS", 3.0),
                    )
        except EdgeTtsError:
            raise
        except Exception as exc:
            raise EdgeTtsError(f"Edge TTS 合成失败: {exc}") from exc
        finally:
            await self._discard_connection()
            if not self._closed:
                self._connect_task = asyncio.create_task(self._connect())

    async def _synthesize(self, websocket, text, on_playback_start):
        import aiohttp
        from edge_tts.communicate import (
            connect_id,
            date_to_string,
            get_headers_and_data,
            mkssml,
            remove_incompatible_characters,
            ssml_headers_plus_data,
        )
        from edge_tts.data_classes import TTSConfig
        from xml.sax.saxutils import escape

        started = time.perf_counter()
        voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
        config = TTSConfig(
            voice,
            os.getenv("EDGE_TTS_RATE", "+0%"),
            os.getenv("EDGE_TTS_VOLUME", "+0%"),
            os.getenv("EDGE_TTS_PITCH", "+0Hz"),
            "SentenceBoundary",
        )
        await websocket.send_str(
            f"X-Timestamp:{date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        escaped = escape(remove_incompatible_characters(text))
        await websocket.send_str(
            ssml_headers_plus_data(
                connect_id(), date_to_string(), mkssml(config, escaped)
            )
        )

        decoder = await asyncio.create_subprocess_exec(
            "mpg123", "-q", "-s", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        player = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", os.getenv(
                "APLAY_DEVICE", "plughw:seeed2micvoicec,0"
            ), "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", "24000",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        first_pcm_at = None
        first_mp3_at = None
        last_mp3_at = None
        pcm_bytes = 0
        volume_percent = env_float("APLAY_VOLUME_PERCENT", 100.0)
        if not 0 < volume_percent <= 200:
            raise ValueError("APLAY_VOLUME_PERCENT 必须在 1～200 之间")

        async def pump_pcm():
            nonlocal first_pcm_at, pcm_bytes, on_playback_start
            assert decoder.stdout is not None and player.stdin is not None
            while True:
                pcm = await decoder.stdout.read(4096)
                if not pcm:
                    break
                if first_pcm_at is None:
                    first_pcm_at = time.perf_counter()
                    if on_playback_start is not None:
                        callback, on_playback_start = on_playback_start, None
                        callback()
                pcm = scale_pcm_s16le(pcm, volume_percent)
                player.stdin.write(pcm)
                await player.stdin.drain()
                pcm_bytes += len(pcm)

        pump_task = asyncio.create_task(pump_pcm())
        try:
            assert decoder.stdin is not None
            async for received in websocket:
                if received.type == aiohttp.WSMsgType.BINARY:
                    header_length = int.from_bytes(received.data[:2], "big")
                    headers, data = get_headers_and_data(received.data, header_length)
                    if headers.get(b"Path") == b"audio" and data:
                        first_mp3_at = first_mp3_at or time.perf_counter()
                        last_mp3_at = time.perf_counter()
                        decoder.stdin.write(data)
                        await decoder.stdin.drain()
                elif received.type == aiohttp.WSMsgType.TEXT:
                    encoded = received.data.encode("utf-8")
                    headers, _ = get_headers_and_data(
                        encoded, encoded.find(b"\r\n\r\n")
                    )
                    if headers.get(b"Path") == b"turn.end":
                        break
                elif received.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    raise EdgeTtsError("Edge TTS WebSocket 提前关闭")
            if first_mp3_at is None:
                raise EdgeTtsError("Edge TTS 没有返回音频")
            decoder.stdin.close()
            await decoder.stdin.wait_closed()
            await decoder.wait()
            await pump_task
            assert player.stdin is not None
            player.stdin.close()
            await player.stdin.wait_closed()
            if await player.wait() != 0:
                raise EdgeTtsError("Edge TTS 播放失败")
        finally:
            if not pump_task.done():
                pump_task.cancel()
            for process in (decoder, player):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        first_pcm_at = first_pcm_at or started
        first_mp3_at = first_mp3_at or started
        last_mp3_at = last_mp3_at or first_mp3_at
        return (
            first_pcm_at - started,
            last_mp3_at - first_mp3_at,
            pcm_bytes / (24000 * 2),
        )

    async def _discard_connection(self) -> None:
        websocket, session = self._websocket, self._session
        self._websocket = None
        self._session = None
        if websocket is not None and not websocket.closed:
            await websocket.close()
        if session is not None and not session.closed:
            await session.close()

    async def _close_async(self) -> None:
        task = self._connect_task
        self._connect_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._discard_connection()
