"""One cancellable playback implementation; callers choose the final mode."""
import asyncio
import csv
import fcntl
import os
from pathlib import Path

from .config import (
    hold_current_and_enable, read_current_action, interpolate_actions,
    startup_transition_seconds, transition_fps, sleep_action,
    sleep_transition_seconds, sleep_hold_seconds, standby_action,
    work_transition_seconds, reading_action, reading_low_action,
    tracking_home_action, tracking_home_transition_seconds,
)

RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"


def load_recording(name, directory=RECORDINGS_DIR):
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("动作名必须是录制名称")
    with (Path(directory) / f"{name}.csv").open(newline="") as file:
        actions = [{k: float(v) for k, v in row.items() if k != "timestamp"}
                   for row in csv.DictReader(file)]
    if not actions:
        raise ValueError(f"动作为空: {name}")
    return actions


class MotionController:
    def __init__(self, port="/dev/ttyACM0", lamp_id="lamppi", fps=30, robot=None):
        if fps <= 0:
            raise ValueError("fps 必须大于 0")
        self.port, self.lamp_id, self.fps = port, lamp_id, fps
        self.robot = robot
        self._device_lock = None
        self.recordings_dir = RECORDINGS_DIR
        self.base_yaw_offset_degrees = 0.0

    def set_base_yaw_offset_degrees(self, degrees):
        self.base_yaw_offset_degrees = float(degrees)

    def _base_yaw_offset_normalized(self):
        """Convert a physical offset to the calibration's -100..100 units."""
        self.connect()
        calibration = self.robot.bus.calibration["base_yaw"]
        model = self.robot.bus.motors["base_yaw"].model
        max_resolution = self.robot.bus.model_resolution_table[model] - 1
        calibrated_degrees = (
            (calibration.range_max - calibration.range_min) * 360.0 / max_resolution
        )
        if calibrated_degrees <= 0:
            raise ValueError("base_yaw 校准范围无效")
        return self.base_yaw_offset_degrees * 200.0 / calibrated_degrees

    def _with_base_heading(self, action):
        shifted = dict(action)
        joint = "base_yaw.pos"
        if joint not in shifted:
            return shifted
        shifted[joint] = float(shifted[joint]) + self._base_yaw_offset_normalized()
        if not -100.0 <= shifted[joint] <= 100.0:
            raise ValueError(
                f"动作叠加当前朝向后超出 base_yaw 校准范围: {shifted[joint]:.2f}"
            )
        return shifted

    def connect(self):
        if self.robot is not None:
            return
        # Guard this app and its legacy playback adapters from opening the same bus.
        lock_path = Path("/tmp") / ("lelamp-" + Path(self.port).name + ".lock")
        lock = lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("舵机正在被另一个 LeLamp 程序使用")
        self._device_lock = lock
        try:
            from lelamp.follower import LeLampFollower, LeLampFollowerConfig
            self.robot = LeLampFollower(LeLampFollowerConfig(port=self.port, id=self.lamp_id))
            self.robot.connect(calibrate=False)
        except BaseException:
            if self.robot is not None and self.robot.bus.is_connected:
                self.robot.bus.disconnect(False)
            self.robot = None
            lock.close()
            self._device_lock = None
            raise

    async def _move_to_target(self, target, duration):
        self.connect()
        hold_current_and_enable(self.robot)
        current = read_current_action(self.robot)
        fps = transition_fps()
        steps = max(1, round(duration * fps))
        for action in interpolate_actions(current, target, steps):
            before = asyncio.get_running_loop().time()
            self.robot.send_action(action)
            await asyncio.sleep(max(0, duration / steps - (asyncio.get_running_loop().time() - before)))

    async def move_to(self, target, duration):
        await self._move_to_target(self._with_base_heading(target), duration)

    async def play(self, name, transition_seconds=None):
        # Transform and validate every frame before the lamp starts moving.
        actions = [self._with_base_heading(action)
                   for action in load_recording(name, self.recordings_dir)]
        if transition_seconds is None:
            transition_seconds = startup_transition_seconds()
        await self._move_to_target(actions[0], transition_seconds)
        for action in actions[1:]:
            before = asyncio.get_running_loop().time()
            self.robot.send_action(action)
            await asyncio.sleep(max(0, 1 / self.fps - (asyncio.get_running_loop().time() - before)))

    async def sleep(self):
        target = sleep_action()
        await self.move_to(target, sleep_transition_seconds())
        await asyncio.sleep(sleep_hold_seconds())
        self.robot.bus.disable_torque()
        print("睡眠动作完成，舵机扭矩已释放。", flush=True)

    async def standby(self, transition_seconds=None):
        """Move to the configured awake pose and keep torque enabled."""
        duration = startup_transition_seconds() if transition_seconds is None else transition_seconds
        await self.move_to(standby_action(), duration)
        print("已进入待机姿态，舵机扭矩保持。", flush=True)

    async def work_pose(self, pose="high", transition_seconds=None):
        """Move to a saved desk-lighting pose and keep torque enabled."""
        if pose not in ("high", "low"):
            raise ValueError("办公姿态必须是 high 或 low")
        target = reading_action() if pose == "high" else reading_low_action()
        duration = work_transition_seconds() if transition_seconds is None else transition_seconds
        await self.move_to(target, duration)
        print(f"已进入办公照明姿态（{pose}），舵机扭矩保持。", flush=True)

    async def tracking_home(self, transition_seconds=None):
        """Enter the camera-forward tracking neutral without saved base heading."""
        duration = (
            tracking_home_transition_seconds()
            if transition_seconds is None else transition_seconds
        )
        await self._move_to_target(tracking_home_action(), duration)
        print("已进入视觉跟踪初始姿态，舵机扭矩保持。", flush=True)

    def read_action(self):
        """Read calibrated joint positions for the single active motion owner."""
        self.connect()
        return read_current_action(self.robot)

    def send_tracking_action(self, action):
        """Send one raw tracking command; callers own limits and cadence."""
        self.connect()
        self.robot.send_action(action)

    async def move_tracking_raw(self, action, duration):
        """Move to an absolute calibrated action for visual calibration only."""
        await self._move_to_target(dict(action), duration)

    def close(self):
        try:
            if self.robot is not None:
                # Failed/cancelled motion must not silently drop the lamp.
                if self.robot.bus.is_connected:
                    self.robot.bus.disconnect(False)
                for camera in self.robot.cameras.values():
                    if camera.is_connected:
                        camera.disconnect()
                self.robot = None
        finally:
            if self._device_lock is not None:
                self._device_lock.close()
                self._device_lock = None
