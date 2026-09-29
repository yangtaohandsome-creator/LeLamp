"""Image-space visual servo for the single motion task owned by LampApp."""

from __future__ import annotations



import asyncio

from collections import deque

from dataclasses import dataclass

import json

import os

from pathlib import Path

import time

from typing import Callable, Protocol



import numpy as np



from lelamp.vision.types import TrackingDirective



from .config import load_motion_config

from . import tracking_diagnostics

from .target_filter import AdaptiveTargetFilter

from .velocity_profile import velocity_step





CALIBRATION_PATH = (

    Path(__file__).resolve().parent / "calibration" / "visual_response.json"

)

CONTROLLED_JOINTS = ("base_yaw", "wrist_pitch")

CONTROLLED_MOTOR_IDS = (1, 4)





class TargetLike(Protocol):

    position_normalized: tuple[float, float]

    velocity_normalized_per_second: tuple[float, float]

    captured_at: float





@dataclass(frozen=True)

class VisualTrackingConfig:

    control_hz: float

    feedback_hz: float

    setpoint: tuple[float, float]

    deadzone: tuple[float, float]

    position_gain: float

    max_prediction_seconds: float

    max_velocity: np.ndarray

    max_acceleration: np.ndarray

    minimum: np.ndarray

    maximum: np.ndarray

    braking_enabled: bool = False

    ego_compensation_enabled: bool = False

    deadzone_transition_width: float = 0.0

    jerk_limit_enabled: bool = False

    max_jerk: tuple[float, float] = (1000.0, 700.0)

    target_filter_enabled: bool = False

    target_filter_min_cutoff: float = 1.5

    target_filter_beta: float = 8.0

    target_filter_error_gain: float = 20.0





def _number(name: str, default: float) -> float:

    return float(os.getenv(name, str(default)))





def load_visual_tracking_config() -> VisualTrackingConfig:

    load_motion_config()

    config = VisualTrackingConfig(

        jerk_limit_enabled=_number("MOTION_TRACKING_JERK_LIMIT_ENABLED", 0) != 0,

        max_jerk=(

            _number("MOTION_TRACKING_MAX_JERK_BASE_YAW", 1000),

            _number("MOTION_TRACKING_MAX_JERK_WRIST_PITCH", 700),

        ),

        target_filter_enabled=_number("MOTION_TRACKING_TARGET_FILTER_ENABLED", 0) != 0,

        target_filter_min_cutoff=_number("MOTION_TRACKING_TARGET_FILTER_MIN_CUTOFF", 1.5),

        target_filter_beta=_number("MOTION_TRACKING_TARGET_FILTER_BETA", 8),

        target_filter_error_gain=_number("MOTION_TRACKING_TARGET_FILTER_ERROR_GAIN", 20),

        deadzone_transition_width=_number("MOTION_TRACKING_DEADZONE_TRANSITION_WIDTH", 0),

        ego_compensation_enabled=_number("MOTION_TRACKING_EGO_COMPENSATION_ENABLED", 0) != 0,

        braking_enabled=_number("MOTION_TRACKING_BRAKING_ENABLED", 0) != 0,

        control_hz=max(1.0, _number("MOTION_TRACKING_CONTROL_HZ", 25)),

        feedback_hz=max(0.5, _number("MOTION_TRACKING_FEEDBACK_HZ", 5)),

        setpoint=(

            _number("MOTION_TRACKING_SETPOINT_X", 0.5),

            _number("MOTION_TRACKING_SETPOINT_Y", 0.42),

        ),

        deadzone=np.array([

            max(0.0, _number("MOTION_TRACKING_DEADZONE_X", 0.035)),

            max(0.0, _number("MOTION_TRACKING_DEADZONE_Y", 0.045)),

        ]),

        position_gain=max(0.0, _number("MOTION_TRACKING_POSITION_GAIN", 2.0)),

        max_prediction_seconds=max(

            0.0, _number("MOTION_TRACKING_MAX_PREDICTION_SECONDS", 0.12)

        ),

        max_velocity=np.array([

            max(0.1, _number("MOTION_TRACKING_MAX_VELOCITY_BASE_YAW", 18)),

            max(0.1, _number("MOTION_TRACKING_MAX_VELOCITY_WRIST_PITCH", 15)),

        ]),

        max_acceleration=np.array([

            max(0.1, _number("MOTION_TRACKING_MAX_ACCELERATION_BASE_YAW", 55)),

            max(0.1, _number("MOTION_TRACKING_MAX_ACCELERATION_WRIST_PITCH", 45)),

        ]),

        minimum=np.array([

            _number("MOTION_TRACKING_MIN_BASE_YAW", -55),

            _number("MOTION_TRACKING_MIN_WRIST_PITCH", 10),

        ]),

        maximum=np.array([

            _number("MOTION_TRACKING_MAX_BASE_YAW", 55),

            _number("MOTION_TRACKING_MAX_WRIST_PITCH", 50),

        ]),

    )

    if not all(0.0 <= item <= 1.0 for item in (*config.setpoint, *config.deadzone)):

        raise ValueError("视觉跟踪画面目标点和死区必须在 0～1")

    if not 0.0 <= config.deadzone_transition_width <= 1.0:

        raise ValueError("死区过渡宽度必须在0～1")

    if (not np.all(np.isfinite([config.target_filter_min_cutoff, config.target_filter_beta, config.target_filter_error_gain]))

            or config.target_filter_min_cutoff <= 0 or config.target_filter_beta < 0

            or config.target_filter_error_gain < 0):

        raise ValueError("目标滤波参数必须有限，截止频率>0，其余>=0")

    if config.target_filter_enabled and not config.ego_compensation_enabled:

        raise ValueError("目标滤波需要实际关节运动补偿")

    if not all(np.isfinite(v) and v > 0 for v in config.max_jerk):

        raise ValueError("视觉跟踪jerk上限必须为有限正数")

    if np.any(config.minimum >= config.maximum):

        raise ValueError("视觉跟踪关节软限位无效")

    return config





def load_response_matrix(path: Path = CALIBRATION_PATH) -> np.ndarray:

    try:

        payload = json.loads(path.read_text(encoding="utf-8"))

    except FileNotFoundError as exc:

        raise RuntimeError(f"尚未完成视觉关节响应标定: {path}") from exc

    joints = tuple(payload.get("controlled_joints", ()))

    if joints != CONTROLLED_JOINTS:

        raise ValueError(f"视觉标定关节不匹配: {joints}")

    motor_ids = tuple(payload.get("controlled_motor_ids", ()))

    if motor_ids != CONTROLLED_MOTOR_IDS:

        raise ValueError(

            f"视觉标定舵机映射不匹配: {motor_ids}，"

            "需要在腕部关节映射修正后重新标定"

        )

    response = np.asarray(payload.get("response_matrix"), dtype=np.float64)

    if response.shape != (2, 2) or not np.all(np.isfinite(response)):

        raise ValueError("视觉标定响应矩阵必须是有限的 2×2 数组")

    singular = np.linalg.svd(response, compute_uv=False)

    if singular[-1] < 1e-5 or singular[0] / singular[-1] > 50:

        raise ValueError("视觉标定响应矩阵退化，需重新标定")

    return response





def smooth_deadzone(error, deadzone, width):

    """Continuous deadzone with scalar slope <=1.5, no temporal filter.



    Subtract the deadzone first, then restore its offset across a broad band.

    Multiplying the full error by a narrow smoothstep magnified local gain.

    width=0 retains the original hard-deadzone rollback.

    """

    error = np.asarray(error, dtype=float)

    magnitude = np.abs(error)

    deadzone = np.asarray(deadzone, dtype=float)

    if width <= 0:

        return np.where(magnitude <= deadzone, 0.0, error)

    excess = np.maximum(0.0, magnitude - deadzone)

    recovery_width = np.maximum(width, 3.0 * deadzone)

    u = np.clip(excess / recovery_width, 0.0, 1.0)

    weight = u * u * (3.0 - 2.0 * u)

    shaped = np.sign(error) * (excess + deadzone * weight)

    return np.where(magnitude >= deadzone + recovery_width, error, shaped)





def braking_velocity(wanted, displacement, velocity, acceleration, age, period):

    """Limit approach speed using delayed-image travel and stopping distance.



    Uses command velocity as an estimate, not a measured joint velocity.

    Braking still passes through the existing acceleration limiter.

    """

    direction = np.sign(displacement)

    closing = np.maximum(0.0, velocity * direction)

    remaining = np.maximum(0.0, np.abs(displacement) - closing * (age + period))

    cap = np.sqrt(2.0 * acceleration * remaining)

    return np.sign(wanted) * np.minimum(np.abs(wanted), cap)





class JointPositionHistory:

    """Measured positions only. Interpolate bracketed history; never extrapolate."""

    def __init__(self):

        self.samples = deque(maxlen=128)



    def add(self, stamp, position):

        position = np.asarray(position, dtype=float)

        if position.shape != (2,) or not np.all(np.isfinite(position)):

            return

        if self.samples and stamp <= self.samples[-1][0]:

            return

        self.samples.append((stamp, position.copy()))



    def displacement(self, captured_at, now, max_gap):

        if len(self.samples) < 2:

            return None

        end, current = self.samples[-1]

        if not self.samples[0][0] <= captured_at <= end <= now:

            return None

        if now - end > max_gap:

            return None

        # Reject a history interrupted by a stale feedback gap.

        relevant = [(t, q) for t, q in self.samples if t >= captured_at]

        if any(b[0] - a[0] > max_gap for a, b in zip(relevant, relevant[1:])):

            return None

        for (ta, qa), (tb, qb) in zip(self.samples, list(self.samples)[1:]):

            if ta <= captured_at <= tb:

                if tb - ta > max_gap:

                    return None

                at_capture = qa + (qb - qa) * ((captured_at - ta) / (tb - ta))

                return current - at_capture, end

        return None





class VisualTrackingRunner:

    """Drive one app-selected vision target without parallel motion sources."""



    def __init__(

        self,

        motion,

        target_provider: Callable[[], TargetLike | TrackingDirective | None],

        *,

        config: VisualTrackingConfig | None = None,

        response_matrix: np.ndarray | None = None,

        start_from_current: bool = False,

    ) -> None:

        self.start_from_current = start_from_current

        self.motion = motion

        self.target_provider = target_provider

        self.config = config or load_visual_tracking_config()

        self.response = (

            np.asarray(response_matrix, dtype=np.float64)

            if response_matrix is not None else load_response_matrix()

        )

        if self.response.shape != (2, 2):

            raise ValueError("response_matrix 必须是 2×2")

        self.inverse = np.linalg.pinv(self.response, rcond=1e-3)

        self.state = {

            "status": "created", "control_hz": self.config.control_hz,

            "commands_sent": 0, "target_visible": False,

        }



    async def run(self) -> None:

        self.state["status"] = "entering_home"

        lower, upper = self.motion.tracking_bounds(

            self.config.minimum, self.config.maximum

        )

        minimum = np.asarray(lower, dtype=np.float64)

        maximum = np.asarray(upper, dtype=np.float64)

        self.state["limits"] = {

            "minimum": minimum.tolist(), "maximum": maximum.tolist(),

        }

        if not self.start_from_current:

            await self.motion.tracking_home()

        current = self.motion.read_action()

        command = np.array([current[f"{name}.pos"] for name in CONTROLLED_JOINTS])

        home_command = command.copy()

        velocity = np.zeros(2, dtype=np.float64)

        acceleration = np.zeros(2, dtype=np.float64)

        self.state["jerk_limit_enabled"] = self.config.jerk_limit_enabled

        period = 1.0 / self.config.control_hz

        feedback_period = 1.0 / (

            max(self.config.feedback_hz, self.config.control_hz)

            if self.config.ego_compensation_enabled else self.config.feedback_hz

        )

        target_filter = AdaptiveTargetFilter(self.config.target_filter_min_cutoff,

            self.config.target_filter_beta, self.config.target_filter_error_gain)

        history = JointPositionHistory()

        history.add(time.monotonic(), command)

        loop = asyncio.get_running_loop()

        recorder = tracking_diagnostics.active_recorder

        if recorder:

            recorder.emit(dict(type='config', config={k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in vars(self.config).items()}, response=self.response.tolist()))

        previous = loop.time()

        next_due = previous

        next_feedback = previous + feedback_period

        self.state["status"] = "running"

        print(

            f"视觉跟踪已启动 | 初始姿态={'current' if self.start_from_current else 'tracking_home'} | "

            f"control={self.config.control_hz:g}Hz",

            flush=True,

        )

        try:

            while True:

                now = loop.time()

                if now < next_due:

                    await asyncio.sleep(next_due - now)

                    now = loop.time()

                dt = min(0.1, max(1e-3, now - previous))

                previous_cycle = previous

                previous = now

                next_due = max(next_due + period, now)



                trace = dict(type='cycle', cycle_start=now, interval=now-previous_cycle, dt=dt) if recorder else None

                try:

                    if now >= next_feedback:

                        read_start = time.monotonic()

                        actual = self.motion.read_action()

                        read_end = time.monotonic()

                        measured = [float(actual[f"{name}.pos"]) for name in CONTROLLED_JOINTS]

                        # A bus read spans time; midpoint approximates the observation time.

                        history.add((read_start + read_end) / 2, measured)

                        self.state["actual"] = [round(item, 3) for item in measured]

                        if trace is not None:

                            trace.update(read_ms=(read_end-read_start)*1000,

                                actual_at=(read_start+read_end)/2, actual=measured)

                        next_feedback += feedback_period

                        if next_feedback < now:

                            next_feedback = now + feedback_period

                    provided = self.target_provider()

                    directive = (

                        provided if isinstance(provided, TrackingDirective)

                        else TrackingDirective("follow", provided, "face")

                    )

                    previous_directive = self.state.get("directive")

                    previous_source = self.state.get("source")

                    if (

                        directive.mode != previous_directive

                        or directive.source != previous_source

                    ):

                        velocity.fill(0.0)

                        acceleration.fill(0.0)

                        target_filter.reset()

                    if trace is not None:

                        trace.update(mode=directive.mode, source=directive.source)

                    self.state["directive"] = directive.mode

                    self.state["source"] = directive.source



                    if directive.mode == "hold":

                        self.state["target_visible"] = False

                        continue



                    if directive.mode == "home":

                        delta = home_command - command

                        arrived = np.abs(delta) <= 0.05

                        command[arrived] = home_command[arrived]

                        velocity[arrived] = 0.0

                        if np.all(arrived):

                            command = home_command.copy()

                            velocity.fill(0.0)

                            self.state["target_visible"] = False

                            continue

                        wanted = np.clip(

                            delta * self.config.position_gain,

                            -self.config.max_velocity, self.config.max_velocity,

                        )

                        dv = np.clip(

                            wanted - velocity,

                            -self.config.max_acceleration * dt,

                            self.config.max_acceleration * dt,

                        )

                        velocity += dv

                        next_command = command + velocity * dt

                        crossed = np.sign(home_command - command) != np.sign(

                            home_command - next_command

                        )

                        command = np.where(crossed, home_command, next_command)

                        command = np.clip(command, minimum, maximum)

                        action = dict(current)

                        for index, name in enumerate(CONTROLLED_JOINTS):

                            action[f"{name}.pos"] = float(command[index])

                        io_start = time.monotonic() if recorder else 0.0

                        self.motion.send_tracking_action(action)

                        if trace is not None:

                            trace.update(write_done=time.monotonic(), write_ms=(time.monotonic()-io_start)*1000, command=command.tolist(), velocity=velocity.tolist())

                        self.state.update({

                            "target_visible": False,

                            "command": [round(float(item), 3) for item in command],

                            "commands_sent": int(self.state["commands_sent"]) + 1,

                        })

                        continue



                    if directive.mode != "follow":

                        raise ValueError(f"未知视觉跟踪指令: {directive.mode}")

                    # Existing work-light poses may lie outside tracking's tighter

                    # envelope. Enter it smoothly only after a follow request, never

                    # clamp a 99 -> 95 position jump into the first servo packet.

                    if self.start_from_current and np.any((command < minimum) | (command > maximum)):

                        safe = np.clip(command, minimum, maximum)

                        distance = np.abs(safe - command)

                        duration = max(.2, float(np.max(2 * distance / self.config.max_velocity)),

                                       float(np.max(np.sqrt(6 * distance / self.config.max_acceleration))))

                        action = dict(self.motion.read_action())

                        action.update({f"{name}.pos": float(value) for name, value in zip(CONTROLLED_JOINTS, safe)})

                        self.state["status"] = "entering_limits"

                        entry = asyncio.create_task(self.motion.move_tracking_raw(action, duration))

                        interrupted = False

                        try:

                            while not entry.done():

                                await asyncio.wait((entry,), timeout=period)

                                if not entry.done():

                                    latest = self.target_provider()

                                    if not isinstance(latest, TrackingDirective) or latest.mode != 'follow' or latest.target is None:

                                        interrupted = True

                                        entry.cancel()

                                        break

                            if not interrupted:

                                await entry

                        finally:

                            if not entry.done():

                                entry.cancel()

                            await asyncio.gather(entry, return_exceptions=True)

                        measured = self.motion.read_action()

                        command = (np.array([measured[f"{name}.pos"] for name in CONTROLLED_JOINTS])

                                   if interrupted else safe.copy())

                        velocity.fill(0)

                        acceleration.fill(0)

                        history = JointPositionHistory()

                        target_filter.reset()

                        self.state["status"] = "running"

                        previous = loop.time()

                        next_due = previous

                        continue

                    target = directive.target

                    if target is None:

                        target_filter.reset()

                        velocity.fill(0.0)

                        acceleration.fill(0.0)

                        self.state["target_visible"] = False

                        continue



                    if trace is not None:

                        trace.update(target_id=getattr(target, "track_id", None), target_at=target.captured_at, target_position=list(target.position_normalized), target_velocity=list(target.velocity_normalized_per_second))

                    age = max(0.0, time.monotonic() - target.captured_at)

                    prediction = min(age, self.config.max_prediction_seconds)

                    position = np.asarray(target.position_normalized, dtype=np.float64)

                    position += np.asarray(

                        target.velocity_normalized_per_second, dtype=np.float64

                    ) * prediction

                    braking_age = age

                    filter_applied = False

                    self.state["target_filter_active"] = False

                    self.state['ego_compensation_active'] = False

                    self.state['ego_image_shift'] = [0.0, 0.0]

                    if (self.config.ego_compensation_enabled and prediction == 0.0

                            and not getattr(target, 'position_is_prediction', False)):

                        correction_time = time.monotonic()

                        movement = history.displacement(target.captured_at, correction_time,

                            max_gap=2.5 * feedback_period)

                        if movement is not None:

                            delta, measured_at = movement

                            shift = self.response @ delta

                            position = position + shift

                            if self.config.target_filter_enabled:

                                # p = stable + R*q. Remove measured camera motion before

                                # estimating target speed; restore CURRENT q every cycle.

                                current_q = history.samples[-1][1]

                                stable = position - self.response @ current_q

                                filtered = target_filter.update(stable, target.captured_at,

                                    (directive.source, getattr(target, "track_id", None)),

                                    np.asarray(self.config.setpoint) - position)

                                position = filtered + self.response @ current_q

                                filter_applied = True

                                self.state["target_filter_active"] = True

                                if trace is not None:

                                    trace.update(filter_input=stable.tolist(),

                                        filter_output=filtered.tolist(),

                                        filter_cutoff=target_filter.cutoff,

                                        filter_target_speed=target_filter.speed.tolist())

                            braking_age = max(0.0, correction_time - measured_at)

                            self.state['ego_compensation_active'] = True

                            self.state['ego_image_shift'] = shift.tolist()

                            if trace is not None:

                                trace.update(ego_joint_delta=delta.tolist(),

                                    ego_image_shift=shift.tolist(), ego_feedback_at=measured_at,

                                    corrected_position=position.tolist(), braking_age=braking_age)

                    if not filter_applied:

                        target_filter.reset()

                    error = np.asarray(self.config.setpoint) - position

                    if trace is not None:

                        trace.update(error_before_deadzone=error.tolist(),

                            inside_deadzone=(np.abs(error) <= self.config.deadzone).tolist())

                    error = smooth_deadzone(error, self.config.deadzone,

                        self.config.deadzone_transition_width)

                    wanted = self.inverse @ (error * self.config.position_gain)

                    if trace is not None:

                        trace.update(error=error.tolist(), wanted_unlimited=wanted.tolist(), age=age)

                    wanted = np.clip(

                        wanted, -self.config.max_velocity, self.config.max_velocity

                    )

                    if trace is not None:

                        trace.update(wanted_speed_limited=wanted.tolist(),

                            velocity_before=velocity.tolist(),

                            ego_compensation_active=self.state['ego_compensation_active'])

                    if self.config.braking_enabled:

                        before_braking = wanted.copy()

                        # Account conservatively for the extra acceleration ramp.

                        ramp_time = (self.config.max_acceleration / np.asarray(self.config.max_jerk)

                                     if self.config.jerk_limit_enabled else 0.0)

                        wanted = braking_velocity(wanted, self.inverse @ error, velocity,

                            self.config.max_acceleration, braking_age + ramp_time, period)

                        self.state['braking_active'] = bool(np.any(np.abs(wanted) < np.abs(before_braking)))

                        if trace is not None:

                            trace.update(wanted_before_braking=before_braking.tolist(), wanted_after_braking=wanted.tolist())

                    dv = np.clip(

                        wanted - velocity,

                        -self.config.max_acceleration * dt,

                        self.config.max_acceleration * dt,

                    )

                    if trace is not None:

                        trace.update(wanted_final=wanted.tolist(),

                            acceleration_limited=(np.abs(wanted - velocity) > self.config.max_acceleration * dt + 1e-9).tolist(),

                            velocity_after_acceleration=(velocity + dv).tolist(),

                            command_before=command.tolist())

                    if self.config.jerk_limit_enabled:

                        before_acceleration = acceleration.copy()

                        displacement, velocity, acceleration = velocity_step(

                            velocity, acceleration, wanted, self.config.max_acceleration,

                            self.config.max_jerk, dt)

                        if trace is not None:

                            trace.update(profile_acceleration_before=before_acceleration.tolist(),

                                profile_acceleration=acceleration.tolist(),

                                profile_velocity=velocity.tolist(),

                                profile_displacement=displacement.tolist(),

                                velocity_after_acceleration=velocity.tolist())

                    else:

                        velocity += dv

                        displacement = velocity * dt

                    proposed = command + displacement

                    command = np.clip(proposed, minimum, maximum)

                    outward = ((command <= minimum + 1e-9) & (velocity < 0)) | (

                        (command >= maximum - 1e-9) & (velocity > 0)

                    )

                    if self.config.jerk_limit_enabled:

                        outward |= command != proposed

                    velocity[outward] = 0.0

                    acceleration[outward] = 0.0

                    action = dict(current)

                    for index, name in enumerate(CONTROLLED_JOINTS):

                        action[f"{name}.pos"] = float(command[index])

                    io_start = time.monotonic() if recorder else 0.0

                    self.motion.send_tracking_action(action)

                    if trace is not None:

                        trace.update(write_done=time.monotonic(), write_ms=(time.monotonic()-io_start)*1000, command=command.tolist(), velocity=velocity.tolist())

                    self.state.update({

                        "target_visible": True,

                        "target_error": [round(float(item), 4) for item in error],

                        "command": [round(float(item), 3) for item in command],

                        "commands_sent": int(self.state["commands_sent"]) + 1,

                    })

                finally:

                    if trace is not None:

                        trace['work_ms'] = (loop.time()-now)*1000

                        recorder.emit(trace)



        finally:

            self.state["status"] = "stopped"

            self.state["target_visible"] = False

            print("视觉跟踪控制循环已停止。", flush=True)
