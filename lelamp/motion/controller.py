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

    async def move_to(self, target, duration):
        self.connect()
        hold_current_and_enable(self.robot)
        current = read_current_action(self.robot)
        fps = transition_fps()
        steps = max(1, round(duration * fps))
        for action in interpolate_actions(current, target, steps):
            before = asyncio.get_running_loop().time()
            self.robot.send_action(action)
            await asyncio.sleep(max(0, duration / steps - (asyncio.get_running_loop().time() - before)))

    async def play(self, name, transition_seconds=None):
        actions = load_recording(name, self.recordings_dir)
        if transition_seconds is None:
            transition_seconds = startup_transition_seconds()
        await self.move_to(actions[0], transition_seconds)
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

    async def standby(self):
        """Move to the configured awake pose and keep torque enabled."""
        await self.move_to(standby_action(), startup_transition_seconds())
        print("已进入待机姿态，舵机扭矩保持。", flush=True)

    async def work_pose(self, pose="high"):
        """Move to a saved desk-lighting pose and keep torque enabled."""
        if pose not in ("high", "low"):
            raise ValueError("办公姿态必须是 high 或 low")
        target = reading_action() if pose == "high" else reading_low_action()
        await self.move_to(target, work_transition_seconds())
        print(f"已进入办公照明姿态（{pose}），舵机扭矩保持。", flush=True)

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
