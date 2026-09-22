"""Lifecycle and fixed-rate joint scheduling for formal vision perception."""
from __future__ import annotations

from collections import deque
import statistics
import threading
import time
from typing import Any, Callable

from .camera import LatestFrameSource
from .config import VisionConfig, load_vision_config
from .target import FaceTargetManager
from .types import TrackingTarget, VisionSnapshot, json_safe_snapshot


class VisionController:
    """Own the camera and model thread without exposing either to LampApp."""

    def __init__(
        self,
        config: VisionConfig | None = None,
        *,
        camera_factory: Callable[[VisionConfig], LatestFrameSource] = LatestFrameSource,
        face_factory=None,
        hands_factory=None,
    ) -> None:
        self.config = config or load_vision_config()
        self._camera_factory = camera_factory
        self._face_factory = face_factory
        self._hands_factory = hands_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera: LatestFrameSource | None = None
        self._snapshot: VisionSnapshot | None = None
        self._tracking_target: TrackingTarget | None = None
        self._target_visible = False
        self._eligible_face_count = 0
        self._status = "disabled" if not self.config.enabled else "stopped"
        self._error: str | None = None
        self._requested = False
        self._completed_times: deque[float] = deque(maxlen=300)
        self._latencies_ms: deque[float] = deque(maxlen=300)
        self._processed_frames = 0
        self._skipped_frames = 0
        self._started_at: float | None = None

    def start(self) -> bool:
        with self._lock:
            self._requested = True
            if not self.config.enabled:
                self._status = "disabled"
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            self._status = "starting"
            self._error = None
            self._snapshot = None
            self._tracking_target = None
            self._target_visible = False
            self._eligible_face_count = 0
            self._completed_times.clear()
            self._latencies_ms.clear()
            self._processed_frames = 0
            self._skipped_frames = 0
            self._started_at = time.monotonic()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="lelamp-vision", daemon=True
            )
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._requested = False
            thread = self._thread
            if thread is None:
                if self.config.enabled:
                    self._status = "stopped"
                return
            self._status = "stopping"
            self._stop.set()
        camera = self._camera
        if camera is not None:
            camera.stop()
        if thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            if thread.is_alive():
                self._status = "error"
                self._error = "视觉线程未在停止期限内退出"
            else:
                self._thread = None
                self._status = "stopped"
                self._tracking_target = None
                self._target_visible = False
                self._eligible_face_count = 0

    def latest_snapshot(self) -> VisionSnapshot | None:
        with self._lock:
            return self._snapshot

    def latest_tracking_target(self) -> TrackingTarget | None:
        with self._lock:
            target = self._tracking_target
            running = self._status == "running"
        if target is None or not running:
            return None
        age_ms = (time.monotonic() - target.captured_at) * 1000.0
        return target if age_ms <= self.config.result_max_age_ms else None

    def state(self) -> dict[str, Any]:
        with self._lock:
            status = self._status
            error = self._error
            requested = self._requested
            processed = self._processed_frames
            skipped = self._skipped_frames
            completed_times = tuple(self._completed_times)
            latencies = tuple(self._latencies_ms)
            snapshot = self._snapshot
            target = self._tracking_target
            target_visible = self._target_visible
            eligible_face_count = self._eligible_face_count
            started_at = self._started_at
        now = time.monotonic()
        recent = [item for item in completed_times if now - item <= 10.0]
        inference_hz = 0.0
        if len(recent) > 1:
            inference_hz = (len(recent) - 1) / (recent[-1] - recent[0])
        sorted_latencies = sorted(latencies)
        p95 = None
        if sorted_latencies:
            p95 = sorted_latencies[min(len(sorted_latencies) - 1, int(len(sorted_latencies) * 0.95))]
        camera = self._camera
        target_state = None
        if target is not None:
            target_age_ms = max(0.0, (now - target.captured_at) * 1000.0)
            target_state = {
                "kind": target.kind,
                "track_id": target.track_id,
                "position_normalized": list(target.position_normalized),
                "velocity_normalized_per_second": list(
                    target.velocity_normalized_per_second
                ),
                "confidence": round(target.confidence, 4),
                "age_ms": round(target_age_ms, 1),
                "expired": target_age_ms > self.config.result_max_age_ms,
                "visible": target_visible,
            }
        return {
            "enabled": self.config.enabled,
            "requested": requested,
            "status": status,
            "running": status == "running",
            "error": error,
            "uptime_seconds": round(now - started_at, 1) if started_at else None,
            "processed_frames": processed,
            "skipped_frames": skipped,
            "inference_hz": round(inference_hz, 2),
            "latency_ms_p50": round(statistics.median(latencies), 1) if latencies else None,
            "latency_ms_p95": round(p95, 1) if p95 is not None else None,
            "result_max_age_ms": self.config.result_max_age_ms,
            "eligible_face_count": eligible_face_count,
            "tracking_target": target_state,
            "snapshot": json_safe_snapshot(snapshot),
            "camera": camera.health() if camera is not None else {
                "status": "stopped", "error": None,
            },
        }

    def _run(self) -> None:
        camera = self._camera_factory(self.config)
        self._camera = camera
        face = None
        hands = None
        targets = FaceTargetManager(self.config)
        try:
            if self._face_factory is None:
                from .face import YuNetFaceDetector
                face_factory = YuNetFaceDetector
            else:
                face_factory = self._face_factory
            if self._hands_factory is None:
                from .hands import MediaPipeHands
                hands_factory = MediaPipeHands
            else:
                hands_factory = self._hands_factory

            camera.start()
            first = camera.wait_for_first_frame(
                self.config.camera_start_timeout_seconds
            )
            if first is None:
                detail = camera.health().get("error") or "摄像头启动超时"
                raise RuntimeError(detail)
            face = face_factory(self.config)
            hands = hands_factory(self.config)
            with self._lock:
                self._status = "running"
            period = 1.0 / self.config.inference_hz
            next_due = time.monotonic()
            previous_sequence = 0
            while not self._stop.is_set():
                now = time.monotonic()
                if now < next_due:
                    self._stop.wait(min(next_due - now, 0.05))
                    continue
                next_due = max(next_due + period, now)
                packet = camera.latest()
                if packet is None or packet.sequence == previous_sequence:
                    continue
                if previous_sequence:
                    with self._lock:
                        self._skipped_frames += max(
                            0, packet.sequence - previous_sequence - 1
                        )
                previous_sequence = packet.sequence

                import cv2
                started = time.monotonic()
                model_image = cv2.resize(
                    packet.image, self.config.model_size,
                    interpolation=cv2.INTER_AREA,
                )
                faces = face.detect(model_image)
                target_update = targets.update(faces, packet.captured_at)
                rgb = cv2.cvtColor(model_image, cv2.COLOR_BGR2RGB)
                hand_results = hands.recognize(
                    rgb, int(packet.captured_at * 1000)
                )
                completed = time.monotonic()
                snapshot = VisionSnapshot(
                    source_frame_sequence=packet.sequence,
                    captured_at=packet.captured_at,
                    completed_at=completed,
                    faces=target_update.faces,
                    hands=hand_results,
                    active_face_target_id=targets.active_track_id,
                )
                with self._lock:
                    self._snapshot = snapshot
                    self._tracking_target = target_update.target
                    self._target_visible = target_update.visible
                    self._eligible_face_count = target_update.eligible_face_count
                    self._processed_frames += 1
                    self._completed_times.append(completed)
                    self._latencies_ms.append((completed - started) * 1000.0)
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            for model in (hands, face):
                if model is not None:
                    try:
                        model.close()
                    except Exception:
                        pass
            camera.stop()
            self._camera = None
            with self._lock:
                if self._stop.is_set():
                    self._status = "stopped"
                self._thread = None
