"""Legacy synchronous service adapter around the app's single motion path."""
import asyncio
import concurrent.futures
import logging
import threading
from pathlib import Path
from lelamp.app import LampApp
from lelamp.motion.controller import MotionController, RECORDINGS_DIR


class MotorsService:
    def __init__(self, port, lamp_id, fps=30):
        self.port, self.lamp_id, self.fps = port, lamp_id, fps
        self.recordings_dir = RECORDINGS_DIR
        self.logger = logging.getLogger("service.motors")
        self._loop = None
        self._thread = None
        self._app = None
        self._future = None

    def start(self):
        if self.is_running:
            return
        self._loop = asyncio.new_event_loop()
        self._app = LampApp(motion=MotionController(self.port, self.lamp_id, self.fps))
        self._app.motion.recordings_dir = Path(self.recordings_dir)
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_pending_event(self):
        return self._future is not None and not self._future.done()

    def dispatch(self, event_type, payload, priority=None):
        if not self.is_running:
            raise RuntimeError("Motion service is not running")
        if event_type != "play":
            raise ValueError(f"Unknown motion event: {event_type}")
        self._future = asyncio.run_coroutine_threadsafe(
            self._app.play_motion(payload), self._loop
        )
        def report(future):
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception:
                self.logger.exception("Motion failed")
        self._future.add_done_callback(report)
        return self._future

    def wait_until_idle(self, timeout=None):
        if self._future is None:
            return True
        try:
            self._future.result(timeout)
        except concurrent.futures.TimeoutError:
            return False
        except concurrent.futures.CancelledError:
            pass
        return True

    def stop(self, timeout=5.0):
        if not self.is_running:
            return
        # Wait for cancellation and parking before closing the serial connection.
        future = asyncio.run_coroutine_threadsafe(self._app.close(), self._loop)
        future.result()  # Hardware shutdown must not race a still-running worker.
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def get_available_recordings(self):
        return sorted(p.stem for p in Path(self.recordings_dir).glob("*.csv"))
