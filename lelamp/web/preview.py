"""Demand-only bounded serialization; no capture, inference or control ownership."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import struct
import time

from aiohttp import web

PREVIEW_MAX_FPS = 5

HEADERS = {'Cache-Control': 'no-store, no-cache, max-age=0', 'Pragma': 'no-cache',
           'X-Content-Type-Options': 'nosniff'}


def pack_preview(item, simulation):
    image, snapshot, face_enabled, _generation = item
    h, w, channels = image.shape
    if channels != 3 or image.dtype.name != 'uint8' or w*h > 640*480:
        raise ValueError('unsupported preview pixels')
    meta = dict(width=w, height=h, format='BGR', sequence=snapshot.source_frame_sequence,
        result_age_ms=snapshot.result_age_ms, simulation=simulation,
        face_detection_enabled=face_enabled, active_face_target_id=snapshot.active_face_target_id,
        active_hand_target_id=snapshot.active_hand_target_id,
        faces=[dict(id=f.track_id, box=f.bbox_normalized) for f in snapshot.faces],
        hands=[dict(id=h.track_id, points=h.landmarks_normalized, gesture=h.gesture,
                    confidence=h.gesture_confidence) for h in snapshot.hands])
    header=json.dumps(meta, separators=(',', ':'), allow_nan=False).encode()
    return struct.pack('!I', len(header))+header+image.tobytes()


class PreviewOutput:
    def __init__(self, app, maintenance):
        self.app, self.maintenance = app, maintenance
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='web-preview')
        self.worker = None
        self.busy = False
        self.next_at = 0.
        self.closed = False

    def current(self):
        if self.closed or self.maintenance.active:
            return None
        item=self.app.vision.latest_model_preview()
        if item is None or item[1].result_age_ms > self.app.vision.config.result_max_age_ms:
            return None
        return item

    async def get(self, request):
        if self.closed:
            return web.Response(status=503, headers=HEADERS)
        if self.busy or (self.worker is not None and not self.worker.done()) or time.monotonic()<self.next_at:
            return web.Response(status=429, headers=HEADERS)
        self.busy=True
        self.next_at=time.monotonic()+1/PREVIEW_MAX_FPS
        try:
            item=self.current()
            if item is None:
                return web.Response(status=503, headers=HEADERS)
            mode_version=self.app._mode_version
            self.worker=asyncio.get_running_loop().run_in_executor(
                self.executor, pack_preview, item, bool(getattr(self.app, 'simulation', False)))
            self.worker.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            payload=await asyncio.shield(self.worker)
            current=self.current()
            if (current is None or self.app._mode_version != mode_version
                    or current[2:] != item[2:]
                    or item[1].result_age_ms > self.app.vision.config.result_max_age_ms):
                return web.Response(status=503, headers=HEADERS)
            # Keep the global slot until this client has accepted the bounded body.
            response=web.StreamResponse(headers={**HEADERS,'Content-Type':'application/octet-stream'})
            response.content_length=len(payload)
            async with asyncio.timeout(1):
                await response.prepare(request)
                await response.write(payload)
                await response.write_eof()
            return response
        except asyncio.CancelledError:
            raise
        except Exception:
            return web.Response(status=503, headers=HEADERS)
        finally:
            self.busy=False

    async def close(self):
        self.closed=True
        if self.worker is not None:
            await asyncio.gather(asyncio.shield(self.worker), return_exceptions=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
