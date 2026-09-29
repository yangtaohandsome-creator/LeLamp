#!/usr/bin/env python3
"""Show live camera perception overlays, optionally with face tracking."""
from __future__ import annotations

import argparse
import asyncio
import os
import multiprocessing
import queue
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from lelamp.vision import VisionController
from lelamp.vision.types import FramePacket, VisionSnapshot


HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)


def disable_display_blanking() -> None:
    """Keep the dedicated Xorg preview display awake for this session."""
    if not os.environ.get("DISPLAY"):
        return
    try:
        for arguments in (("s", "off"), ("s", "noblank"), ("-dpms",)):
            subprocess.run(
                ("xset", *arguments),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未安装xset；请先安装系统包x11-xserver-utils"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or f"exit={exc.returncode}"
        raise RuntimeError(f"无法关闭显示器自动熄屏: {detail}") from exc
    print("显示器自动熄屏和DPMS省电已关闭。", flush=True)


class GuidedHandCalibration:
    STEPS = (
        ("prepare_boundary", "准备：把手放到你希望刚好还能启动跟随的最远距离", 6, None),
        ("boundary_open", "保持该距离，手掌正对镜头并张开", 6, "Open_Palm"),
        ("switch_boundary_fist", "保持距离不变，现在握拳", 3, None),
        ("boundary_fist", "保持该距离和握拳姿势", 6, "Closed_Fist"),
        ("turn_boundary", "保持该距离，手掌稍微转向一侧并张开", 4, None),
        ("boundary_angle_open", "保持轻微侧转和张开姿势", 6, "Open_Palm"),
        ("switch_boundary_angle_fist", "保持轻微侧转，现在握拳", 3, None),
        ("boundary_angle_fist", "保持轻微侧转和握拳姿势", 6, "Closed_Fist"),
        ("move_far", "把手移到比启动边界更远的位置，并张开", 5, None),
        ("far_open", "保持较远距离和张开姿势", 6, "Open_Palm"),
        ("switch_far_fist", "保持较远距离，现在握拳", 3, None),
        ("far_fist", "保持较远距离和握拳姿势", 6, "Closed_Fist"),
    )

    def __init__(self) -> None:
        self.index = 0
        self.step_started_at: float | None = None
        self.last_countdown = None
        self.samples: dict[str, list[tuple[float, str | None, float]]] = {
            label: [] for label, _, _, expected in self.STEPS if expected is not None
        }

    def update(self, snapshot: VisionSnapshot, now: float) -> bool:
        if self.step_started_at is None:
            self.step_started_at = now
            print("\n=== 手部近距离引导标定开始；按终端提示动作 ===", flush=True)
            self._announce(now)
        label, _, duration, expected = self.STEPS[self.index]
        elapsed = now - self.step_started_at
        if elapsed >= duration:
            self.index += 1
            self.step_started_at = now
            self.last_countdown = None
            if self.index >= len(self.STEPS):
                self._summary()
                return True
            self._announce(now)
            label, _, _, expected = self.STEPS[self.index]
        else:
            self._announce(now)

        if expected is not None and snapshot.hands:
            # The guided procedure asks for one foreground hand. If another hand
            # appears, the largest palm is the intended calibration sample.
            hand = max(snapshot.hands, key=lambda item: item.palm_scale)
            self.samples[label].append((
                hand.palm_scale, hand.gesture, hand.gesture_confidence
            ))
        return False

    def current_instruction(self) -> str:
        return self.STEPS[self.index][1]

    def _announce(self, now: float) -> None:
        _, instruction, duration, _ = self.STEPS[self.index]
        remaining = max(0, int(duration - (now - self.step_started_at) + 0.999))
        if remaining == self.last_countdown:
            return
        self.last_countdown = remaining
        print(
            f"[步骤 {self.index + 1}/{len(self.STEPS)}] {instruction} "
            f"| 剩余 {remaining} 秒",
            flush=True,
        )

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        return ordered[round((len(ordered) - 1) * fraction)]

    def _summary(self) -> None:
        print("\n=== HAND_CALIBRATION_SUMMARY ===", flush=True)
        summary = {}
        for label, _, _, expected in self.STEPS:
            if expected is None:
                continue
            samples = self.samples[label]
            scales = [item[0] for item in samples]
            matched = [item for item in samples if item[1] == expected]
            confidences = [item[2] for item in matched]
            if not samples:
                print(f"{label}: samples=0", flush=True)
                summary[label] = {"samples": 0, "expected": expected}
                continue
            row = {
                "samples": len(samples),
                "expected": expected,
                "matched": len(matched),
                "scale_p10": self._percentile(scales, 0.10),
                "scale_median": statistics.median(scales),
                "scale_p90": self._percentile(scales, 0.90),
                "confidence_p10": (
                    self._percentile(confidences, 0.10) if confidences else None
                ),
                "confidence_median": (
                    statistics.median(confidences) if confidences else None
                ),
            }
            summary[label] = row
            print(
                f"{label}: samples={len(samples)} expected={expected} "
                f"matched={len(matched)} "
                f"scale_p10={row['scale_p10']:.6f} "
                f"scale_median={row['scale_median']:.6f} "
                f"scale_p90={row['scale_p90']:.6f} "
                + (
                    f"confidence_p10={row['confidence_p10']:.5f} "
                    f"confidence_median={row['confidence_median']:.5f}"
                    if confidences else "confidence=no_matching_gesture"
                ),
                flush=True,
            )
        boundary_rows = [
            summary[name] for name in (
                "boundary_open", "boundary_fist",
                "boundary_angle_open", "boundary_angle_fist",
            )
        ]
        far_rows = [summary[name] for name in ("far_open", "far_fist")]
        if any(not row.get("matched") for row in (*boundary_rows, *far_rows)):
            raise RuntimeError("标定失败：至少一个采样阶段没有识别到预期手势")
        boundary_floor = min(row["scale_p10"] for row in boundary_rows)
        far_ceiling = max(row["scale_p90"] for row in far_rows)
        if far_ceiling >= boundary_floor:
            raise RuntimeError(
                "标定失败：启动边界与较远负样本没有可靠尺度间隔，请重新采样"
            )
        gap = boundary_floor - far_ceiling
        enter_scale = boundary_floor - gap * 0.20
        exit_scale = far_ceiling + gap * 0.40
        confidence_floor = min(
            row["confidence_p10"] for row in (*boundary_rows, *far_rows)
        )
        gesture_confidence = max(0.30, min(0.90, confidence_floor - 0.05))
        from lelamp.vision.config import save_hand_calibration
        destination = save_hand_calibration(
            enter_scale, exit_scale, gesture_confidence, summary
        )
        print(
            "推荐阈值已覆盖保存："
            f"enter={enter_scale:.6f} exit={exit_scale:.6f} "
            f"gesture_confidence={gesture_confidence:.5f}",
            flush=True,
        )
        print(f"保存位置：{destination}", flush=True)
        print("=== 标定采样结束，程序将自动退出 ===\n", flush=True)


def _point(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    return round(x * width), round(y * height)


def _label(image, text: str, origin: tuple[int, int], color) -> None:
    x, y = origin
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.46
    thickness = 1
    (label_width, label_height), _ = cv2.getTextSize(text, font, scale, thickness)
    y = max(label_height + 4, y)
    cv2.rectangle(
        image, (x, y - label_height - 4),
        (x + label_width + 8, y + 4), (16, 20, 28), -1,
    )
    cv2.putText(image, text, (x + 4, y), font, scale, color, thickness, cv2.LINE_AA)


def render_preview(
    packet: FramePacket, snapshot: VisionSnapshot, inference_hz: float,
    setpoint: tuple[float, float], tracking_state: dict | None = None,
):
    """Draw observations on their exact source frame, not a newer capture."""
    image = packet.image.copy()
    height, width = image.shape[:2]
    active = snapshot.active_face_target_id
    active_hand = snapshot.active_hand_target_id
    gesture_progress = {
        track_id: (gesture, progress)
        for track_id, gesture, progress in snapshot.gesture_progress
    }

    for face in snapshot.faces:
        x, y, box_width, box_height = face.bbox_normalized
        left, top = _point(x, y, width, height)
        right, bottom = _point(x + box_width, y + box_height, width, height)
        selected = face.track_id is not None and face.track_id == active
        color = (70, 235, 80) if selected else (0, 205, 255)
        cv2.rectangle(image, (left, top), (right, bottom), color, 2)
        ax, ay = _point(*face.anchor_normalized, width, height)
        cv2.circle(image, (ax, ay), 4, color, -1)
        caption = f"{'TARGET ' if selected else ''}FACE {face.confidence:.2f}"
        if face.track_id:
            caption += f" {face.track_id}"
        _label(image, caption, (max(0, left), top - 5), color)

    for hand in snapshot.hands:
        points = [_point(x, y, width, height) for x, y in hand.landmarks_normalized]
        if not points:
            continue
        selected = hand.track_id is not None and hand.track_id == active_hand
        color = (80, 255, 255) if selected else (255, 140, 255)
        for first, second in HAND_CONNECTIONS:
            if first < len(points) and second < len(points):
                cv2.line(image, points[first], points[second], color, 2)
        for point in points:
            cv2.circle(image, point, 3, (255, 255, 255), -1)
        left = max(0, min(point[0] for point in points))
        right = min(width - 1, max(point[0] for point in points))
        top = max(0, min(point[1] for point in points))
        bottom = min(height - 1, max(point[1] for point in points))
        cv2.rectangle(image, (left, top), (right, bottom), color, 1)
        gesture = hand.gesture or "unknown"
        if hand.gesture_candidates is not None:
            gesture = "Fused=" + gesture
        handedness = hand.handedness or "hand"
        progress = gesture_progress.get(hand.track_id)
        progress_text = (
            f" confirm={progress[1] * 100:.0f}%" if progress is not None else ""
        )
        _label(
            image,
            f"{'TARGET ' if selected else ''}{handedness} "
            f"{hand.track_id or '-'}: {gesture} {hand.gesture_confidence:.2f} "
            f"scale={hand.palm_scale:.4f}{progress_text}",
            (left, max(45, top - 6)), color,
        )
        if hand.finger_angles:
            _label(
                image,
                "3D angles T/I/M/R/P: "
                + "/".join(f"{angle:.0f}" for angle in hand.finger_angles),
                (left, min(height - 10, bottom + 19)), color,
            )

    age_ms = snapshot.result_age_ms
    tracking_state = tracking_state or {}
    lines = (
        f"LeLamp VISION | inference {inference_hz:.1f} Hz | result {age_ms:.0f} ms",
        f"faces {len(snapshot.faces)} | hands {len(snapshot.hands)} | "
        f"target {active or '-'} | phase {tracking_state.get('tracking_phase', '-')} "
        f"| calibrated {tracking_state.get('hand_control_calibrated', False)}",
    )
    cv2.rectangle(image, (0, 0), (width, 48), (16, 20, 28), -1)
    for index, line in enumerate(lines):
        cv2.putText(
            image, line, (7, 18 + index * 21),
            cv2.FONT_HERSHEY_SIMPLEX, 0.41, (255, 255, 255), 1, cv2.LINE_AA,
        )
    if age_ms > 500:
        _label(image, "STALE RESULT", (8, height - 10), (0, 0, 255))
    aim = _point(*setpoint, width, height)
    cv2.circle(image, aim, 16, (0, 0, 0), 4)
    cv2.circle(image, aim, 15, (255, 255, 0), 2)
    cv2.drawMarker(
        image, aim, (255, 255, 0), cv2.MARKER_CROSS, 24, 2, cv2.LINE_AA
    )
    label_x = min(max(0, aim[0] + 20), max(0, width - 190))
    label_y = min(height - 10, aim[1] + 26)
    _label(
        image, f"SETPOINT ({setpoint[0]:.2f}, {setpoint[1]:.2f})",
        (label_x, label_y), (255, 255, 0),
    )
    return image


def fit_screen(image, screen_width: int, screen_height: int):
    height, width = image.shape[:2]
    scale = min(screen_width / width, screen_height / height)
    output_width = round(width * scale)
    output_height = round(height * scale)
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    enlarged = cv2.resize(image, (output_width, output_height), interpolation=interpolation)
    canvas = np.zeros((screen_height, screen_width, 3), dtype=np.uint8)
    x = (screen_width - output_width) // 2
    y = (screen_height - output_height) // 2
    canvas[y:y + output_height, x:x + output_width] = enlarged
    return canvas


def _display_worker(frames, closed, errors, windowed, width, height, timings):
    """Own all GUI calls in a separate process; never access hardware."""
    try:
        cv2.namedWindow("LeLamp Vision", cv2.WINDOW_NORMAL)
        if not windowed:
            cv2.resizeWindow("LeLamp Vision", width, height)
            cv2.moveWindow("LeLamp Vision", 0, 0)
            cv2.setWindowProperty("LeLamp Vision", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        disable_display_blanking()
        while not closed.is_set():
            try:
                payload = frames.get(timeout=.01)
            except queue.Empty:
                payload = None
            if payload is not None:
                received = time.monotonic()
                payload, enqueued, measured = payload
                image = render_preview(*payload)
                rendered = time.monotonic()
                cv2.imshow("LeLamp Vision", image if windowed else fit_screen(image, width, height))
            key = cv2.waitKey(1)
            if payload is not None and measured:
                try:
                    timings.put_nowait(dict(type='display', seq=payload[0].sequence,
                        captured_at=payload[0].captured_at, enqueued=enqueued,
                        received=received, rendered=rendered, submitted=time.monotonic()))
                except queue.Full:
                    pass
            if key & 0xFF in (27, ord('q')):
                break
    except Exception as exc:
        errors.put(str(exc))
    finally:
        closed.set()
        cv2.destroyAllWindows()


async def run(args: argparse.Namespace) -> None:
    from lelamp.vision import latency
    from lelamp.motion.visual_tracking import load_visual_tracking_config

    setpoint = load_visual_tracking_config().setpoint
    app = None
    vision = None
    display_process = None
    context = multiprocessing.get_context('spawn')
    display_frames = context.Queue(maxsize=1)
    display_errors = context.Queue()
    display_timings = context.Queue(maxsize=256)
    display_closed = context.Event()
    last_sequence = -1
    last_report = 0.0
    stop = asyncio.Event()
    calibration = GuidedHandCalibration() if args.hand_calibration else None
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        if args.follow:
            from lelamp.app import LampApp
            from lelamp.location import load_cached_location
            from lelamp.tools import ToolSource
            from lelamp.voice.config import load_voice_config

            load_voice_config()
            load_cached_location()
            app = LampApp()
            await app.start()
            result = await app.tools.execute(
                "start_face_tracking", source=ToolSource.CONTROL_API
            )
            if result.status != "completed":
                raise RuntimeError(result.message)
            vision = app.vision
        else:
            vision = VisionController()
            if not vision.start():
                raise RuntimeError("vision.conf 未启用视觉")
        deadline = time.monotonic() + 15
        while not stop.is_set():
            while True:
                try:
                    timing = display_timings.get_nowait()
                except queue.Empty:
                    break
                if latency.recorder is not None:
                    latency.recorder.emit(timing)
            if display_process is not None:
                if display_closed.is_set() or not display_process.is_alive():
                    try:
                        error = display_errors.get_nowait()
                    except queue.Empty:
                        error = None
                    if error or display_process.exitcode not in (None, 0):
                        raise RuntimeError(error or "预览显示进程异常退出")
                    break
            if app is not None:
                motion_task = app.current_motion_task
                if motion_task is not None and motion_task.done():
                    motion_task.result()
                    raise RuntimeError("跟随任务提前退出")
            state = vision.state()
            if state["status"] == "error":
                raise RuntimeError(state.get("error") or "视觉线程失败")
            preview = vision.latest_preview()
            if preview is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("15 秒内未收到视觉结果")
                await asyncio.sleep(0.03)
                continue
            packet, snapshot = preview
            if packet.sequence == last_sequence:
                await asyncio.sleep(0.01)
                continue
            last_sequence = packet.sequence
            tracking_state = (
                app.get_vision_state().get("tracking_session") or {}
                if app is not None else {}
            )
            calibration_done = False
            if calibration is not None:
                calibration_done = calibration.update(snapshot, time.monotonic())
            if args.snapshot is not None:
                image = render_preview(packet, snapshot, state['inference_hz'], setpoint, tracking_state)
                args.snapshot.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(args.snapshot), image):
                    raise RuntimeError(f"预览图保存失败: {args.snapshot}")
                print(f"已保存预览图: {args.snapshot}", flush=True)
                break
            if display_process is None:
                display_process = context.Process(target=_display_worker,
                    args=(display_frames, display_closed, display_errors, args.windowed,
                          args.screen_width, args.screen_height, display_timings), daemon=True)
                display_process.start()
                print('独立显示进程已启动；运动循环不执行绘图或waitKey。', flush=True)
            try:
                display_frames.put_nowait(((packet, snapshot, state['inference_hz'], setpoint, tracking_state), time.monotonic(), latency.recorder is not None))
            except queue.Full:
                # Display can drop frames; motion must never wait for the GUI.
                pass
            if calibration_done:
                break
            if calibration is None and time.monotonic() - last_report >= 3:
                print(
                    f"preview | inference={state['inference_hz']:.1f}Hz "
                    f"faces={len(snapshot.faces)} hands={len(snapshot.hands)} "
                    f"hands_detail={[(hand.track_id, hand.gesture, round(hand.gesture_confidence, 3), round(hand.palm_scale, 5)) for hand in snapshot.hands]}"
                    f" finger_angles={[(hand.track_id, tuple(round(angle) for angle in hand.finger_angles)) for hand in snapshot.hands]}"
                    + (
                        f" tracking={app.get_vision_state()['motion_tracking']}"
                        if app is not None else ""
                    ),
                    flush=True,
                )
                last_report = time.monotonic()
    finally:
        if app is not None:
            await app.close()
        elif vision is not None:
            vision.stop()
        display_closed.set()
        if display_process is not None:
            await asyncio.to_thread(display_process.join, 3)
            if display_process.is_alive():
                display_process.terminate()
                await asyncio.to_thread(display_process.join, 2)
        display_frames.cancel_join_thread()
        display_frames.close()
        display_errors.close()
        display_timings.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="保存一帧标注图后退出，用于无屏检查")
    parser.add_argument("--windowed", action="store_true", help="不用全屏窗口")
    parser.add_argument("--follow", action="store_true", help="同时通过 LampApp 启动人脸机械跟随")
    parser.add_argument(
        "--hand-calibration", action="store_true",
        help="在终端逐步引导并汇总手势置信度和掌骨尺度；不启用机械跟随",
    )
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    parser.add_argument("--tracking-log", type=Path, help="记录运动控制诊断JSONL（文件不可已存在）")
    args = parser.parse_args()
    if args.snapshot is not None and args.follow:
        parser.error("--snapshot 和 --follow 不能同时使用")
    if args.hand_calibration and args.follow:
        parser.error("--hand-calibration 和 --follow 不能同时使用")
    if args.screen_width <= 0 or args.screen_height <= 0:
        parser.error("屏幕尺寸必须为正整数")
    if args.tracking_log is not None and not args.follow:
        parser.error("--tracking-log 需要 --follow")
    if args.tracking_log is None:
        asyncio.run(run(args))
    else:
        from lelamp.motion import tracking_diagnostics
        args.tracking_log.parent.mkdir(parents=True, exist_ok=True)
        recorder = tracking_diagnostics.Recorder(args.tracking_log)
        tracking_diagnostics.active_recorder = recorder
        print(f"运动诊断记录：{args.tracking_log}", flush=True)
        try:
            asyncio.run(run(args))
        finally:
            tracking_diagnostics.active_recorder = None
            recorder.close()
            print(f"记录结束：丢记录={recorder.dropped}，写入错误={recorder.error}", flush=True)


if __name__ == "__main__":
    main()
