"""Exclusive, operator-driven recording and calibration for the LAN console.

The app owns entry/exit. This module only owns the maintenance serial device
and files; it never starts the voice loop or chooses a robot mode.
"""
from __future__ import annotations

import asyncio
import copy
import csv
from dataclasses import asdict
import fcntl
import json
import math
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from ..motion.config import CONFIG_PATH, motion_port
from ..motion.controller import RECORDINGS_DIR, load_recording

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_JOINTS = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
_POSES = {"sleep": "SLEEP", "standby": "STANDBY", "high": "READING", "low": "READING_LOW"}


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    directory = path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"{path.stem}-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


class MaintenanceController:
    def __init__(self, *, device_factory=None, recordings_dir=RECORDINGS_DIR,
                 config_path=CONFIG_PATH, heartbeat_seconds=15.0, lock_path=None):
        self.device_factory = device_factory
        self.lock_path = Path(lock_path) if lock_path is not None else None
        self.recordings_dir = Path(recordings_dir)
        self.config_path = Path(config_path)
        self.heartbeat_seconds = heartbeat_seconds
        self.active = False
        self.kind: str | None = None
        self.device = None
        self._device_lock = None
        self._sampler: asyncio.Task | None = None
        self._sampling = False
        self._samples: list[dict] = []
        self._range_min: dict[str, int] = {}
        self._range_max: dict[str, int] = {}
        self._homing: dict[str, int] | None = None
        self.last_contact = time.monotonic()
        self.error: str | None = None
        self._io_lock = asyncio.Lock()
        self._preview_task = None
        self._baseline = None
        self._dirty = False
        self._recovery = {}
        self._captured_pose = None
        self._disconnected = False

    async def _io(self, function, *args, **kwargs):
        async with self._io_lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise

    def _require_idle(self):
        if not self.active:
            raise ValueError("请先进入维护模式")
        if self._sampling or self._preview_task is not None:
            raise ValueError("请先停止当前采样或预览")

    def state(self) -> dict:
        return {"active": self.active, "device": self.kind,
                "sampling": self._sampling, "samples": len(self._samples),
                "homing_ready": self._homing is not None,
                "range_ready": bool(self._range_min) and not self._sampling,
                "error": self.error, "disconnected": self._disconnected,
                "calibration_dirty": self._dirty,
                "recoverable": list(self._recovery),
                "previewing": self._preview_task is not None}

    def touch(self):
        self.last_contact = time.monotonic()

    async def begin(self):
        if self.active:
            raise ValueError("维护模式已经开启")
        self.active = True
        self.touch()
        self.error = None
        self._disconnected = False
        self._captured_pose = None

    async def disconnect_browser(self):
        # A disconnected operator must never cause an automatic power-on/move.
        await self.stop_sampling()
        if self._preview_task is not None:
            self._preview_task.cancel()
            await asyncio.gather(self._preview_task, return_exceptions=True)
        await self.release_device()
        self._disconnected = True
        self.error = "网页已断开；保持维护模式和无扭矩状态，重连后确认退出"

    async def check_heartbeat(self):
        if self.active and not self._disconnected and time.monotonic() - self.last_contact > self.heartbeat_seconds:
            await self.disconnect_browser()

    def _make_device(self, kind: str):
        if self.device_factory is not None:
            return self.device_factory(kind)
        if kind == "leader":
            from ..leader import LeLampLeader, LeLampLeaderConfig
            return LeLampLeader(LeLampLeaderConfig(port=motion_port(), id=os.getenv("MOTION_LAMP_ID", "lamppi")))
        from ..follower import LeLampFollower, LeLampFollowerConfig
        return LeLampFollower(LeLampFollowerConfig(port=motion_port(), id=os.getenv("MOTION_LAMP_ID", "lamppi")))

    def _open_sync(self, kind: str):
        path = self.lock_path or (Path("/tmp") / ("lelamp-" + Path(motion_port()).name + ".lock"))
        lock = path.open("a")
        device = None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            device = self._make_device(kind)
            device.connect(calibrate=False)
            device.bus.disable_torque()
            self.device, self.kind, self._device_lock = device, kind, lock
            self._baseline = copy.deepcopy(device.bus.read_calibration())
            self._homing = None
            self._range_min, self._range_max = {}, {}
        except BaseException:
            if device is not None and device.bus.is_connected:
                try:
                    device.bus.disable_torque()
                    device.bus.disconnect(False)
                except BaseException:
                    self.device, self.kind, self._device_lock = device, kind, lock
                    raise
            self.device, self.kind, self._device_lock = None, None, None
            lock.close()
            raise

    async def open_device(self, kind: str):
        self._require_idle()
        if kind not in {"leader", "follower"}:
            raise ValueError("设备必须是 leader 或 follower")
        self.touch()
        if self.device is not None:
            if self.kind == kind:
                return self.state()
            await self.release_device()
        await self._io(self._open_sync, kind)
        self._disconnected = False
        return self.state()

    def _release_sync(self):
        device, lock = self.device, self._device_lock
        if device is not None and device.bus.is_connected:
            device.bus.disable_torque()
            if self._dirty and self._baseline is not None:
                device.bus.write_calibration(self._baseline)
                self._dirty = False
            device.bus.disconnect(False)
        self.device, self.kind, self._device_lock = None, None, None
        self._homing = None
        if lock is not None:
            lock.close()

    async def release_device(self):
        await self.stop_sampling()
        if self.device is not None:
            await self._io(self._release_sync)

    async def stop_sampling(self):
        self._sampling = False
        task, self._sampler = self._sampler, None
        if task is not None:
            await task

    async def end(self):
        if self._preview_task is not None:
            raise ValueError("请先停止预览")
        await self.stop_sampling()
        await self.release_device()
        self.active = False
        self.error = None
        self._homing = None
        self._captured_pose = None
        return self.state()

    async def start_recording(self):
        self._require_idle()
        if self._dirty:
            raise ValueError("请先保存或恢复校准")
        await self.open_device("leader")
        if self._sampling:
            raise ValueError("正在采样")
        self._samples = []
        self._sampling = True
        self._sampler = asyncio.create_task(self._sample_recording())
        return self.state()

    async def _sample_recording(self):
        try:
            while self._sampling:
                now = time.perf_counter()
                sample = await self._io(self.device.get_action)
                self._samples.append({"timestamp": now, **sample})
                if len(self._samples) >= 18000:
                    self.error = "已达到 10 分钟录制上限，请停止并保存"
                    self._sampling = False
                await asyncio.sleep(max(0, 1 / 30 - (time.perf_counter() - now)))
        except Exception as exc:
            self.error = str(exc)
            self._sampling = False

    async def save_recording(self, name: str, overwrite: bool = False):
        if not self.active:
            raise ValueError("请先进入维护模式")
        await self.stop_sampling()
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("动作名只能是字母、数字和下划线，须以字母开头")
        if len(self._samples) < 2:
            raise ValueError("采样不足，至少需要两帧")
        expected = {joint + ".pos" for joint in _JOINTS}
        for frame in self._samples:
            if set(frame) - {"timestamp"} != expected or any(
                not math.isfinite(float(frame[key])) or not -100 <= float(frame[key]) <= 100
                for key in expected
            ):
                raise ValueError("录制包含无效关节数据，请检查校准")
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        target = self.recordings_dir / f"{name}.csv"
        if target.exists() and not overwrite:
            raise ValueError("动作已存在，明确勾选覆盖后才能重录")
        saved_backup = backup(target) if target.exists() else None
        temp = target.with_suffix(".csv.tmp")
        try:
            with temp.open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(self._samples[0]))
                writer.writeheader()
                writer.writerows(self._samples)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        return {"name": name, "frames": len(self._samples), "backup": str(saved_backup) if saved_backup else None}

    async def preview(self, name: str, motion):
        self._require_idle()
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("动作名无效")
        load_recording(name, self.recordings_dir)
        await self.release_device()
        async def play():
            try:
                await motion.play(name)
                await motion.sleep()
            finally:
                try:
                    if motion.robot is not None:
                        motion.robot.bus.disable_torque()
                finally:
                    motion.close()
        self._preview_task = asyncio.create_task(play())
        try:
            await self._preview_task
        finally:
            self._preview_task = None
        return {"name": name, "previewed": True}

    async def center(self):
        self._require_idle()
        if self.kind not in {"follower", "leader"}:
            raise ValueError("请先打开校准设备")
        # This writes hardware homing offsets, so it is an explicit operator step.
        if not self._dirty:
            path = Path(self.device.calibration_fpath)
            self._recovery[self.kind] = (copy.deepcopy(self._baseline), path, backup(path))
        self._dirty = True
        self._homing = await self._io(self.device.bus.set_half_turn_homings)
        self._range_min = {}
        self._range_max = {}
        return {"homing_offsets": self._homing}

    async def start_ranges(self):
        self._require_idle()
        if self._homing is None:
            raise ValueError("请先摆中位并记录中心")
        if self._sampling:
            raise ValueError("正在采样")
        self._sampling = True
        self._sampler = asyncio.create_task(self._sample_ranges())
        return self.state()

    async def _sample_ranges(self):
        try:
            while self._sampling:
                values = await self._io(self.device.bus.sync_read, "Present_Position", normalize=False)
                for joint, value in values.items():
                    value = int(value)
                    self._range_min[joint] = min(value, self._range_min.get(joint, value))
                    self._range_max[joint] = max(value, self._range_max.get(joint, value))
                await asyncio.sleep(0.05)
        except Exception as exc:
            self.error = str(exc)
            self._sampling = False

    async def calibration_result(self):
        await self.stop_sampling()
        if not self._range_min:
            raise ValueError("尚未记录关节范围")
        return {"homing_offsets": self._homing, "range_min": self._range_min,
                "range_max": self._range_max}

    async def save_calibration(self):
        result = await self.calibration_result()
        device = self.device
        if device is None or self.kind not in {"follower", "leader"}:
            raise ValueError("校准设备未打开")
        from lerobot.motors import MotorCalibration
        previous = copy.deepcopy(self._baseline)
        path = Path(device.calibration_fpath)
        saved_backup = self._recovery[self.kind][2]
        calibration = {}
        for joint, motor in device.bus.motors.items():
            low, high = result["range_min"][joint], result["range_max"][joint]
            if high <= low:
                raise ValueError(f"{joint} 没有记录有效活动范围")
            calibration[joint] = MotorCalibration(
                id=motor.id, drive_mode=0, homing_offset=result["homing_offsets"][joint],
                range_min=low, range_max=high)
        try:
            await self._io(device.bus.write_calibration, calibration)
            device.calibration = calibration
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            try:
                temp.write_text(json.dumps({k: asdict(v) for k, v in calibration.items()}, indent=2))
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            self._baseline = copy.deepcopy(calibration)
            self._dirty = False
        except BaseException:
            device.calibration = previous
            if saved_backup is not None:
                shutil.copy2(saved_backup, path)
            if previous:
                await self._io(device.bus.write_calibration, previous)
                self._dirty = False
            raise
        return {"device": self.kind, "backup": str(saved_backup) if saved_backup else None,
                "calibration": result}

    async def restore_calibration(self, kind):
        self._require_idle()
        if kind not in self._recovery:
            raise ValueError("本次维护没有可恢复的校准备份")
        await self.open_device(kind)
        hardware, path, saved = self._recovery[kind]
        await self._io(self.device.bus.write_calibration, hardware)
        if saved is not None:
            shutil.copy2(saved, path)
        else:
            path.unlink(missing_ok=True)
        self._baseline = copy.deepcopy(hardware)
        self._dirty = False
        self._homing = None
        self.device.calibration = copy.deepcopy(hardware)
        return {"restored": kind}

    async def capture_pose(self, name: str):
        self._require_idle()
        if self._dirty:
            raise ValueError("请先保存或恢复校准")
        if name not in _POSES:
            raise ValueError("姿态只能是 sleep、standby、high 或 low")
        await self.open_device("follower")
        action = await self._io(self.device.get_observation)
        prefix = "MOTION_" + _POSES[name] + "_"
        values = {prefix + joint.upper(): float(action[joint + ".pos"]) for joint in _JOINTS}
        self._captured_pose = {"pose": name, "values": values}
        return copy.deepcopy(self._captured_pose)

    def save_pose(self, name: str, values: dict):
        self._require_idle()
        if self._captured_pose != {"pose": name, "values": values}:
            raise ValueError("只能保存刚刚读取的姿态，请重新读取")
        if name not in _POSES:
            raise ValueError("姿态名无效")
        prefix = "MOTION_" + _POSES[name] + "_"
        keys = {prefix + joint.upper() for joint in _JOINTS}
        if set(values) != keys:
            raise ValueError("必须提交全部五个关节的当前姿态")
        converted = {key: float(value) for key, value in values.items()}
        if any(not -100 <= value <= 100 for value in converted.values()):
            raise ValueError("姿态值须在校准坐标 -100～100 内")
        lines = self.config_path.read_text().splitlines(keepends=True)
        found = set()
        updated = []
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if key in converted:
                updated.append(f"{key}={converted[key]}\n")
                found.add(key)
            else:
                updated.append(line)
        if found != keys:
            raise ValueError("motion.conf 缺少姿态关节配置")
        saved_backup = backup(self.config_path)
        temp = self.config_path.with_suffix(".conf.tmp")
        try:
            temp.write_text("".join(updated))
            temp.replace(self.config_path)
        finally:
            temp.unlink(missing_ok=True)
        return {"pose": name, "values": converted, "backup": str(saved_backup)}
