import os
import time
from pathlib import Path
from typing import Dict, Iterator

from dotenv import load_dotenv


CONFIG_PATH = Path(__file__).resolve().parents[2] / "motion.conf"


def load_motion_config() -> None:
    load_dotenv(CONFIG_PATH, override=True)


def startup_transition_seconds() -> float:
    load_motion_config()
    return max(0.0, float(os.getenv("MOTION_STARTUP_TRANSITION_SECONDS", "3.0")))


def active_transition_seconds() -> float:
    load_motion_config()
    return max(0.0, float(os.getenv("MOTION_ACTIVE_TRANSITION_SECONDS", "0.5")))


def transition_fps() -> int:
    load_motion_config()
    return max(1, int(os.getenv("MOTION_TRANSITION_FPS", "30")))


def sleep_transition_seconds() -> float:
    load_motion_config()
    return max(0.0, float(os.getenv("MOTION_SLEEP_TRANSITION_SECONDS", "3.0")))


def sleep_hold_seconds() -> float:
    load_motion_config()
    return max(0.0, float(os.getenv("MOTION_SLEEP_HOLD_SECONDS", "1.0")))


def work_transition_seconds() -> float:
    load_motion_config()
    return max(0.0, float(os.getenv("MOTION_WORK_TRANSITION_SECONDS", "1.5")))


def auto_expression_enabled() -> bool:
    load_motion_config()
    return os.getenv("AUTO_EXPRESSION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def auto_expression_in_work_light() -> bool:
    load_motion_config()
    return os.getenv("AUTO_EXPRESSION_IN_WORK_LIGHT", "0").strip().lower() in {"1", "true", "yes", "on"}


def sleep_action() -> Dict[str, float]:
    load_motion_config()
    motors = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
    return {f"{motor}.pos": float(os.environ[f"MOTION_SLEEP_{motor.upper()}"])
            for motor in motors}


def standby_action() -> Dict[str, float]:
    load_motion_config()
    motors = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
    return {f"{motor}.pos": float(os.environ[f"MOTION_STANDBY_{motor.upper()}"])
            for motor in motors}


def reading_action() -> Dict[str, float]:
    """Return the saved desk-lighting/read pose."""
    load_motion_config()
    motors = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
    return {f"{motor}.pos": float(os.environ[f"MOTION_READING_{motor.upper()}"])
            for motor in motors}


def reading_low_action() -> Dict[str, float]:
    """Return the saved low-light reading pose."""
    load_motion_config()
    motors = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
    return {f"{motor}.pos": float(os.environ[f"MOTION_READING_LOW_{motor.upper()}"])
            for motor in motors}



def read_current_action(robot) -> Dict[str, float]:
    positions = robot.bus.sync_read("Present_Position")
    return {f"{motor}.pos": float(value) for motor, value in positions.items()}


def interpolate_actions(
    start: Dict[str, float], target: Dict[str, float], steps: int
) -> Iterator[Dict[str, float]]:
    for index in range(1, steps + 1):
        progress = index / steps
        smooth = progress * progress * (3.0 - 2.0 * progress)
        yield {
            joint: start.get(joint, target_value)
            + (target_value - start.get(joint, target_value)) * smooth
            for joint, target_value in target.items()
        }


def move_smoothly(robot, target: Dict[str, float], duration: float, message: str) -> None:
    fps = transition_fps()
    if duration <= 0:
        robot.send_action(target)
        return

    start = read_current_action(robot)
    steps = max(1, round(duration * fps))
    started = time.perf_counter()
    print(f"{message}（{duration:.2f} 秒）...")
    for index, action in enumerate(interpolate_actions(start, target, steps), start=1):
        robot.send_action(action)
        remaining = started + index / fps - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)


def hold_current_and_enable(robot) -> None:
    current = robot.bus.sync_read("Present_Position", normalize=False)
    robot.bus.sync_write("Goal_Position", current, normalize=False)
    robot.bus.enable_torque()


def move_to_first_action(robot, target: Dict[str, float], enable_torque: bool = False) -> None:
    if enable_torque:
        hold_current_and_enable(robot)
    move_smoothly(robot, target, startup_transition_seconds(), "正在平滑进入动作起始姿态")


def sleep_robot(robot) -> None:
    move_smoothly(robot, sleep_action(), sleep_transition_seconds(), "正在进入睡眠姿态")
    hold_seconds = sleep_hold_seconds()
    if hold_seconds > 0:
        print(f"睡眠姿态到位，保持 {hold_seconds:.2f} 秒后释放扭矩...")
        time.sleep(hold_seconds)
    robot.bus.disable_torque()
    print("已到达睡眠姿态，舵机扭矩已释放。")
