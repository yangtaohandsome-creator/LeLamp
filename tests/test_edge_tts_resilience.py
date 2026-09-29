import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import aiohttp
from lelamp.voice.edge_tts import EdgeSpeechClient, EdgeTtsError, EdgeTtsPlaybackError
from lelamp.voice.playback import PlaybackControl, SpeechInterrupted
from lelamp.voice import tts

class Process:
    def __init__(self, decoder=False):
        self.returncode = None
        self.queue = asyncio.Queue()
        self.stdin = Mock()
        self.stdin.drain = AsyncMock()
        self.stdin.wait_closed = AsyncMock()
        self.stdin.close.side_effect = lambda: self.queue.put_nowait(b'')
        if decoder:
            self.stdin.write.side_effect = lambda _: self.queue.put_nowait(b'\0' * 400)
        self.stdout = SimpleNamespace(read=lambda _: self.queue.get())
    def kill(self):
        self.returncode = -9
        self.queue.put_nowait(b'')
    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

class Socket:
    closed = False
    def __init__(self, messages):
        self.messages = list(messages)
        self.send_str = AsyncMock()
    async def receive(self):
        await asyncio.sleep(0)
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()

HEADER = b'X:1\r\nPath:audio\r\n'
AUDIO = SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=len(HEADER).to_bytes(2,'big')+HEADER+b'mp3')
END = SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data='Path:turn.end\r\n\r\n')

class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'EDGE_TTS_DIAGNOSTICS':'0',
            'EDGE_TTS_FIRST_AUDIO_TIMEOUT_SECONDS':'.03', 'EDGE_TTS_RECEIVE_TIMEOUT_SECONDS':'.03',
            'EDGE_TTS_PRECONNECT_MAX_AGE_SECONDS':'20'})
        self.env.start()
        self.c = EdgeSpeechClient.__new__(EdgeSpeechClient)
        self.c._connected_at = time.monotonic()
        self.c._websocket = Socket([])
        self.c._connect_task = None
        self.c._closed = True  # suppress speculative connection after a test
        self.c._discard_connection = AsyncMock()
    def tearDown(self):
        self.env.stop()
    async def synth(self, messages, control=None):
        self.processes = [Process(True), Process()]
        with patch('asyncio.create_subprocess_exec', new=AsyncMock(side_effect=self.processes)):
            return await self.c._synthesize(Socket(messages), '测试', None, control)
    async def test_first_audio_timeout_cleans_both_children(self):
        with self.assertRaisesRegex(EdgeTtsError, '首个音频包'):
            await self.synth([])
        self.assertTrue(all(p.returncode is not None for p in self.processes))
    async def test_metadata_cannot_keep_first_audio_alive(self):
        metadata = SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                   data='Path:audio.metadata\r\n\r\n')
        with self.assertRaisesRegex(EdgeTtsError, '首个音频包'):
            await self.synth([metadata] * 10000)
        self.assertTrue(all(p.returncode is not None for p in self.processes))

    async def test_partial_audio_missing_end_never_replays(self):
        with self.assertRaises(EdgeTtsPlaybackError):
            await self.synth([AUDIO])
        self.assertTrue(all(p.returncode is not None for p in self.processes))
    async def test_success_and_next_request_after_failure(self):
        with self.assertRaises(EdgeTtsError):
            await self.synth([])
        result = await self.synth([AUDIO, END])
        self.assertGreater(result[2], 0)
        self.assertTrue(all(p.returncode == 0 for p in self.processes))
    async def test_interrupt_is_not_timeout_or_retry(self):
        control = PlaybackControl()
        task = asyncio.create_task(self.synth([], control))
        await asyncio.sleep(.005)
        control.cancel()
        with self.assertRaises(SpeechInterrupted):
            await task
        self.assertTrue(all(p.returncode is not None for p in self.processes))
    async def test_fresh_connection_reused_and_expired_refreshed(self):
        original = self.c._websocket
        self.c._connect = AsyncMock()
        self.assertIs(await self.c._take_ready_connection(), original)
        self.c._connect.assert_not_called()
        self.c._connected_at -= 25
        async def connect():
            self.c._websocket = Socket([])
            self.c._connected_at = time.monotonic()
        self.c._connect.side_effect = connect
        self.assertIsNot(await self.c._take_ready_connection(), original)
        self.c._connect.assert_awaited_once()
    async def test_preconnect_never_replaces_connection_during_active_speech(self):
        self.c._closed = False
        self.c._connected_at -= 60
        self.c._synthesizing = True
        self.c._connect = AsyncMock()
        self.assertFalse(await self.c._prepare())
        self.c._connect.assert_not_called()
        self.c._discard_connection.assert_not_called()

    async def test_empty_audio_failure_retries_once_but_partial_does_not(self):
        self.c._take_ready_connection = AsyncMock(return_value=Socket([]))
        self.c._connect = AsyncMock(return_value=Socket([]))
        self.c._synthesize = AsyncMock(side_effect=[EdgeTtsError('timeout'), (1,2,3)])
        self.assertEqual(await self.c._speak('测试',None,None),(1,2,3))
        self.assertEqual(self.c._synthesize.await_count,2)
        self.c._synthesize = AsyncMock(side_effect=EdgeTtsPlaybackError('partial'))
        with self.assertRaises(EdgeTtsPlaybackError):
            await self.c._speak('测试',None,None)
        self.c._synthesize.assert_awaited_once()
    async def test_partial_failure_does_not_fall_back_to_remote(self):
        with patch.dict(os.environ, {'TTS_BACKEND':'edge','TTS_FALLBACK_BACKEND':'remote'}), \
             patch.object(tts,'_get_edge_client') as edge, patch.object(tts,'_speak_remote') as remote:
            edge.return_value.speak.side_effect = EdgeTtsPlaybackError('partial')
            with self.assertRaises(EdgeTtsPlaybackError):tts.speak('测试')
            remote.assert_not_called()
