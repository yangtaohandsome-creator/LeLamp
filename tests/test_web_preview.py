import asyncio
import json
import struct
import threading
import time
import unittest
from unittest.mock import patch, AsyncMock, Mock

import test_web
from lelamp.web.preview import pack_preview


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_web.WebTests.asyncSetUp
    asyncTearDown = test_web.WebTests.asyncTearDown
    async def test_raw_same_frame_and_read_only(self):
        self.app.vision.start()
        result=await self.client.get('/api/v1/web/vision/preview')
        self.assertEqual(result.status,200)
        data=await result.read();n=struct.unpack('!I',data[:4])[0]
        meta=json.loads(data[4:4+n]);pixels=data[4+n:]
        self.assertEqual(len(pixels),meta['width']*meta['height']*3)
        self.assertEqual(pixels[:3],bytes((255,0,0)))
        self.assertTrue(meta['simulation'])
        self.assertEqual(len(meta['hands'][0]['points']),21)
        self.assertEqual(self.app.motion.events,[])
        self.assertFalse(self.app._web_claimed)
        self.assertIn('no-store',result.headers['Cache-Control'])
        second=await self.client.get('/api/v1/web/vision/preview')
        self.assertEqual(second.status,429)

    async def test_five_fps_budget_reopens_after_200ms(self):
        self.app.vision.start()
        first=await self.client.get('/api/v1/web/vision/preview')
        self.assertEqual(first.status,200)
        await first.read()
        self.assertEqual((await self.client.get('/api/v1/web/vision/preview')).status,429)
        await asyncio.sleep(.22)
        second=await self.client.get('/api/v1/web/vision/preview')
        self.assertEqual(second.status,200)
        await second.read()

    async def test_unavailable_and_cross_origin_do_not_start_vision(self):
        r=await self.client.get('/api/v1/web/vision/preview')
        self.assertEqual(r.status,503);self.assertFalse(self.app.vision.running)
        r=await self.client.get('/api/v1/web/vision/preview',headers={'Origin':'http://other.example'})
        self.assertEqual(r.status,403)

    async def test_stop_maintenance_and_failure_isolated(self):
        self.app.vision.start();self.app.maintenance.active=True
        r=await self.client.get('/api/v1/web/vision/preview');self.assertEqual(r.status,503)
        self.app.maintenance.active=False;self.console.preview.next_at=0
        with patch('lelamp.web.preview.pack_preview',side_effect=RuntimeError('broken')):
            r=await self.client.get('/api/v1/web/vision/preview');self.assertEqual(r.status,503)
        self.assertTrue(self.app.vision.running)
        r=await self.client.get('/api/v1/web/vision');self.assertEqual(r.status,200)

    async def test_inflight_stop_discards_old_frame_and_parallel_rejected(self):
        self.app.vision.start();entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(2);return pack_preview(*args)
        with patch('lelamp.web.preview.pack_preview',side_effect=slow):
            first=asyncio.create_task(self.client.get('/api/v1/web/vision/preview'))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():await asyncio.sleep(.01)
                r=await self.client.get('/api/v1/web/vision/preview');self.assertEqual(r.status,429)
                self.app.vision.stop();release.set()
                self.assertEqual((await first).status,503)
            finally:release.set();await first

    async def test_cancelled_request_does_not_queue_second_worker(self):
        self.app.vision.start();entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(2);return pack_preview(*args)
        with patch('lelamp.web.preview.pack_preview',side_effect=slow) as pack:
            first=asyncio.create_task(self.console.preview.get(None))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():await asyncio.sleep(.01)
                first.cancel();await asyncio.gather(first,return_exceptions=True)
                self.console.preview.next_at=0
                r=await self.console.preview.get(None)
                self.assertEqual(r.status,429);self.assertEqual(pack.call_count,1)
            finally:release.set();await self.console.preview.close()

    async def test_policy_change_discards_inflight_faces(self):
        self.app.vision.start();entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(2);return pack_preview(*args)
        with patch('lelamp.web.preview.pack_preview',side_effect=slow):
            first=asyncio.create_task(self.client.get('/api/v1/web/vision/preview'))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():await asyncio.sleep(.01)
                self.app.vision.set_face_detection_enabled(False);release.set()
                self.assertEqual((await first).status,503)
            finally:release.set();await first

    async def test_slow_transport_times_out_without_stopping_vision(self):
        self.app.vision.start()
        response=Mock()
        response.prepare=AsyncMock()
        async def blocked_write(_):
            await asyncio.sleep(5)
        response.write=AsyncMock(side_effect=blocked_write)
        response.write_eof=AsyncMock()
        with patch('lelamp.web.preview.web.StreamResponse',return_value=response):
            result=await asyncio.wait_for(self.console.preview.get(None),1.5)
        self.assertEqual(result.status,503)
        self.assertFalse(self.console.preview.busy)
        self.assertTrue(self.app.vision.running)
        self.assertEqual(self.app.motion.events,[])
