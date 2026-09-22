"""Single-owner UVC capture that exposes only the newest frame."""
from __future__ import annotations

import threading
import time
from typing import Any

from .config import VisionConfig
from .types import FramePacket


class LatestFrameSource:
    def __init__(self, config: VisionConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: Any = None
        self._latest: FramePacket | None = None
        self._sequence = 0
        self._status = "stopped"
        self._error: str | None = None
        self._read_failures = 0
        self._negotiated: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._status = "starting"
            self._error = None
        self._thread = threading.Thread(
            target=self._capture_loop, name="lelamp-camera", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        capture = self._capture
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        self._capture = None
        with self._lock:
            self._status = "stopped"

    def latest(self) -> FramePacket | None:
        with self._lock:
            return self._latest

    def wait_for_first_frame(self, timeout: float) -> FramePacket | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            packet = self.latest()
            if packet is not None:
                return packet
            with self._lock:
                if self._status == "error":
                    return None
            time.sleep(0.02)
        return self.latest()

    def health(self) -> dict[str, Any]:
        with self._lock:
            packet = self._latest
            status = self._status
            error = self._error
            failures = self._read_failures
            negotiated = dict(self._negotiated)
        age_ms = None
        if packet is not None:
            age_ms = max(0.0, (time.monotonic() - packet.captured_at) * 1000.0)
        return {
            "status": status,
            "error": error,
            "latest_sequence": packet.sequence if packet else None,
            "latest_frame_age_ms": round(age_ms, 1) if age_ms is not None else None,
            "read_failures": failures,
            "negotiated": negotiated,
        }

    def _capture_loop(self) -> None:
        try:
            import cv2

            backend = cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") else cv2.CAP_ANY
            capture = cv2.VideoCapture(self.config.camera, backend)
            self._capture = capture
            if not capture.isOpened():
                raise RuntimeError(f"无法打开摄像头 {self.config.camera}")
            capture.set(
                cv2.CAP_PROP_FOURCC,
                cv2.VideoWriter_fourcc(*self.config.capture_fourcc),
            )
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.capture_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.capture_height)
            capture.set(cv2.CAP_PROP_FPS, self.config.capture_fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            actual_fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
            decoded_fourcc = "".join(
                chr((actual_fourcc >> (8 * index)) & 0xFF) for index in range(4)
            )
            raw_width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
            raw_height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            output_width, output_height = (
                (raw_height, raw_width)
                if self.config.rotation in (90, 270)
                else (raw_width, raw_height)
            )
            with self._lock:
                self._negotiated = {
                    "width": raw_width,
                    "height": raw_height,
                    "fps": round(float(capture.get(cv2.CAP_PROP_FPS)), 2),
                    "fourcc": decoded_fourcc,
                    "rotation": self.config.rotation,
                    "output_width": output_width,
                    "output_height": output_height,
                }
                self._status = "running"

            consecutive_failures = 0
            while not self._stop.is_set():
                ok, image = capture.read()
                captured_at = time.monotonic()
                if not ok or image is None:
                    consecutive_failures += 1
                    with self._lock:
                        self._read_failures += 1
                    if consecutive_failures >= 30:
                        raise RuntimeError("摄像头连续读取失败 30 帧")
                    time.sleep(0.02)
                    continue
                consecutive_failures = 0
                if self.config.rotation:
                    rotations = {
                        90: cv2.ROTATE_90_CLOCKWISE,
                        180: cv2.ROTATE_180,
                        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                    }
                    image = cv2.rotate(image, rotations[self.config.rotation])
                height, width = image.shape[:2]
                with self._lock:
                    self._sequence += 1
                    self._latest = FramePacket(
                        sequence=self._sequence,
                        captured_at=captured_at,
                        capture_size=(width, height),
                        model_input_size=self.config.model_size,
                        rotation=self.config.rotation,
                        mirrored=False,
                        image=image,
                    )
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            capture = self._capture
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            self._capture = None
            if self._stop.is_set():
                with self._lock:
                    self._status = "stopped"
