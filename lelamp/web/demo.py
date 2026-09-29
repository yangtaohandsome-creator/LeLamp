"""Local simulation adapters; never open hardware, Agent, or TTS services."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from ..app import LampApp, ActionResult
from ..alarm import AlarmManager
from ..todo import TodoManager
from ..motion.config import CONFIG_PATH
from .maintenance import MaintenanceController, _JOINTS


class DemoBus:
    def __init__(self):
        from lerobot.motors import MotorCalibration
        self.is_connected = False
        self.motors = {name: SimpleNamespace(id=i + 1) for i, name in enumerate(_JOINTS)}
        self.calibration = {name: MotorCalibration(id=i + 1, drive_mode=0,
            homing_offset=0, range_min=0, range_max=4095) for i, name in enumerate(_JOINTS)}
        self.samples = 0
        self.torque = False

    def disable_torque(self):
        self.torque = False

    def disconnect(self, *_):
        self.is_connected = False

    def read_calibration(self):
        return copy.deepcopy(self.calibration)

    def write_calibration(self, value):
        self.calibration = copy.deepcopy(value)

    def set_half_turn_homings(self):
        return {name: 0 for name in _JOINTS}

    def sync_read(self, *_args, normalize=True):
        self.samples += 1
        value = (self.samples % 50) / 5 if normalize else 1800 + self.samples % 400
        return {name: value for name in _JOINTS}


class DemoDevice:
    def __init__(self, root: Path, kind: str):
        self.bus = DemoBus()
        self.calibration = self.bus.read_calibration()
        self.calibration_fpath = root / f"{kind}.json"
        self.cameras = {}

    def connect(self, **_):
        self.bus.is_connected = True

    def get_action(self):
        return {f"{k}.pos": v for k, v in self.bus.sync_read().items()}

    get_observation = get_action


class DemoMotion:
    def __init__(self, root):
        self.robot = None
        self.events = []
        self.recordings_dir = root / "recordings"

    def set_base_yaw_offset_degrees(self, degrees):
        self.heading = degrees

    async def standby(self, *args, **_):
        self.events.append("standby")

    async def sleep(self):
        self.events.append("sleep")

    async def work_pose(self, pose, *args, **_):
        self.events.append(f"work:{pose}")

    async def play(self, name, *args, **_):
        self.events.append(f"play:{name}")
        await asyncio.sleep(0.03)

    async def move_to(self, target, duration):
        self.events.append("move")

    async def move_tracking_raw(self, target, duration):
        self.events.append("restore_work_position")

    def read_action(self):
        return {f"{k}.pos": 0 for k in _JOINTS}

    def close(self):
        self.robot = None


class DemoLighting:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class DemoVision:
    def __init__(self):
        from dataclasses import replace
        from ..vision.config import load_vision_config
        self.config = replace(load_vision_config(), enabled=True, work_light_hand_enabled=True,
                              hand_near_enter_scale=.2, hand_near_exit_scale=.17, gesture_min_confidence=.5)
        self.gesture = 'Closed_Fist'
        self.running = False
        self.calibrated = True
        self.hand_visible = True
        self.error = None

    def set_active_hand_target(self, target):
        pass

    def latest_snapshot(self):
        from ..vision.types import HandObservation, VisionSnapshot
        if not self.running:
            return None
        stamp = int(time.monotonic() * 10) / 10
        hands = (HandObservation('demo-hand', 'Left', (), (.5, .5), self.gesture, .9, .3),) if self.hand_visible else ()
        return VisionSnapshot(int(stamp * 10), stamp, stamp, (), hands)

    def latest_model_preview(self):
        if not self.running or self.error:
            return None
        import numpy as np
        from dataclasses import replace
        if not hasattr(self, '_preview_pixels'):
            self._preview_pixels = np.zeros((320,240,3), dtype=np.uint8)
            self._preview_pixels[:160,:120] = (255,0,0)
            self._preview_pixels[:160,120:] = (0,255,0)
            self._preview_pixels[160:] = (0,0,255)
            self._preview_pixels.flags.writeable = False
        snap=self.latest_snapshot()
        hands=tuple(replace(h, landmarks_normalized=tuple(
            (.25+(i%4)*.12,.3+(i//4)*.08) for i in range(21))) for h in snap.hands)
        return self._preview_pixels, replace(snap,hands=hands), getattr(self,'face_detection_enabled',True), getattr(self,'face_detection_enabled',True)

    def latest_tracking_inputs(self):
        from ..vision.types import HandTrackingCandidate, TrackingTarget
        snap = self.latest_snapshot()
        candidates = tuple(HandTrackingCandidate(h.track_id,
            TrackingTarget('hand', h.track_id, (.5, .5), (0, 0), snap.captured_at, .9),
            h.palm_scale, h.handedness, h.gesture, h.gesture_confidence, True, snap.captured_at)
            for h in snap.hands) if snap else ()
        return snap, None, False, candidates

    def set_face_detection_enabled(self, enabled):
        self.face_detection_enabled = bool(enabled)

    def start(self):
        self.running = True

    def state(self):
        running = self.running and not self.error
        return {"enabled": True, "running": running,
                "face_detection_enabled": getattr(self, "face_detection_enabled", True),
                "status": "error" if self.error else "running" if running else "stopped",
                "error": self.error, "inference_hz": 10 if running else 0,
                "result_max_age_ms": 250, "hand_control_calibrated": self.calibrated,
                "camera": {"status": "running" if running else "stopped"},
                "snapshot": {"result_age_ms": 20, "face_count": int(getattr(self, "face_detection_enabled", True)),
                    "hand_count": int(self.hand_visible),
                    "hands": [{"track_id": "demo-hand", "gesture": "Closed_Fist",
                               "gesture_confidence": .9}] if self.hand_visible else []} if running else None}

    def stop(self):
        self.running = False


class DemoTrackingSession:
    def __init__(self):
        self.state = {}
        self.force_face()

    def force_face(self):
        self.state.update(tracking_phase="face_follow", tracking_source="face")

    def request_hand(self):
        self.requested_at = time.monotonic()
        self.state.update(tracking_phase="hand_acquire", tracking_source="face")

    def discard_pending_events(self):
        pass


class DemoApp(LampApp):
    simulation = True

    def __init__(self, root: Path):
        os.environ.setdefault("LELAMP_TIMEZONE", "Asia/Shanghai")
        super().__init__(motion=DemoMotion(root), lighting=DemoLighting(), vision=DemoVision())
        self.alarms = AlarmManager(self._on_alarm_complete, root=root)
        self.todos = TodoManager(root)
        self.spoken = []
        self.maintenance = MaintenanceController(
            device_factory=lambda kind: DemoDevice(root, kind),
            recordings_dir=root / "recordings", config_path=root / "motion.conf",
            lock_path=root / "demo-device.lock")
        (root / "motion.conf").write_text(CONFIG_PATH.read_text())

    def _make_work_light_runner(self, session):
        class Runner:
            state = {'status': 'created'}
            async def run(inner):
                inner.state = {'status': 'running'}
                try:
                    while True:
                        directive = session.directive()
                        inner.state.update(directive=directive.mode, target_visible=bool(directive.target))
                        await asyncio.sleep(.04)
                finally:
                    inner.state['status'] = 'stopped'
        return Runner()

    async def _sync_vision_lifecycle(self):
        self.vision.set_face_detection_enabled(self.current_mode != 'work_light')
        if self._voice_transition is not None and not self._voice_transition_valid():
            self._voice_transition = None
            self._voice_transition_started = None
        if self._vision_should_run():
            self.vision.start()
        else:
            self.vision.stop()

    async def start_face_tracking(self):
        await self._start_demo_tracking(False)

    async def start_hand_tracking(self):
        if self.current_mode == "work_light":
            return await super().start_hand_tracking()
        if not self.vision.calibrated:
            raise ValueError("手部近距离和手势置信度阈值尚未标定")
        await self._start_demo_tracking(True)

    async def _start_demo_tracking(self, hand):
        if self.current_mode != "tracking" or self._tracking_session is None:
            self._tracking_session = DemoTrackingSession()
            runner = SimpleNamespace(state={"status": "created"})
            self._visual_tracking_runner = runner
            session = self._tracking_session
            async def run():
                runner.state["status"] = "entering_home"
                try:
                    await asyncio.sleep(.1)
                    runner.state["status"] = "running"
                    while True:
                        if session.state["tracking_phase"] == "hand_acquire":
                            age = time.monotonic() - session.requested_at
                            if self.vision.hand_visible and age >= 2.5:
                                session.state.update(tracking_phase="hand_follow", tracking_source="hand")
                            elif age >= 5:
                                session.force_face()
                        runner.state.update(directive="hold" if session.state["tracking_phase"] == "hand_hold" else "follow",
                                            target_visible=session.state["tracking_phase"] != "hand_hold")
                        await asyncio.sleep(.1)
                finally:
                    runner.state["status"] = "stopped"
            if hand:
                session.request_hand()
            await self.set_mode("tracking", run)
        elif hand:
            self._tracking_session.request_hand()
        else:
            self._tracking_session.force_face()

    async def handle_text(self, text, session_id):
        return ActionResult(f"本机模拟已收到：{text}。实机启动后会交给 Agent 并播报。")

    async def _speak_now(self, text, expression):
        self.spoken.append(text)
        await asyncio.sleep(0.03)
        return (0.0, 0.0, 0.03)

    async def _play_sound_now(self, cue):
        return 0.0

    async def resume_voice_after_web(self):
        # A demo has no microphone or voice worker.
        self._voice_pause_requested = False

    async def turn_base(self, direction="left", steps=1):
        delta = (30 if direction == "left" else -30) * steps
        target = self.base_heading_degrees + delta
        if abs(target) > 60:
            raise ValueError("已到朝向边界")
        self.base_heading_degrees = target
        return target

    async def reset_base_heading(self):
        self.base_heading_degrees = 0
        return 0

    async def set_base_heading(self, position):
        if position not in {"left", "front", "right"}:
            raise ValueError("朝向无效")
        self.base_heading_degrees = {"left":60, "front":0, "right":-60}[position]
        return self.base_heading_degrees
