#!/usr/bin/env python3
"""独立的人脸/手势实时测试程序。

本程序只读取摄像头并在终端输出识别结果，不连接 LampApp、不控制舵机。
默认路径对应 Pi5 上的 ~/lelamp_vision_eval 实验环境。
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys
import time
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LeLamp 独立实时人脸/手势测试")
    parser.add_argument("--camera", default="/dev/video0", help="摄像头设备路径或编号，默认 /dev/video0")
    parser.add_argument("--width", type=int, default=640, help="采集宽度，默认 640")
    parser.add_argument("--height", type=int, default=480, help="采集高度，默认 480")
    parser.add_argument("--fps", type=float, default=30.0, help="请求摄像头帧率，默认 30")
    parser.add_argument("--input-width", type=int, help="模型输入宽度，默认与采集宽度相同")
    parser.add_argument("--input-height", type=int, help="模型输入高度，默认与采集高度相同")
    parser.add_argument(
        "--fourcc", default="MJPG", choices=("MJPG", "YUYV"), help="摄像头像素格式，默认 MJPG"
    )
    parser.add_argument("--hands", type=int, default=2, choices=(1, 2), help="最多识别几只手，默认 2")
    parser.add_argument(
        "--models",
        type=Path,
        default=Path.home() / "lelamp_vision_eval" / "models",
        help="模型目录，默认 ~/lelamp_vision_eval/models",
    )
    parser.add_argument("--no-face", action="store_true", help="只运行手势识别，不运行 YuNet")
    return parser.parse_args()


def camera_source(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def create_gesture_recognizer(model_path: Path, hands: int):
    options = vision.GestureRecognizerOptions(
        base_options=python.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=hands,
        min_hand_detection_confidence=0.5,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return vision.GestureRecognizer.create_from_options(options)


def main() -> int:
    args = parse_args()
    input_width = args.input_width or args.width
    input_height = args.input_height or args.height
    if (args.input_width is None) != (args.input_height is None):
        print("--input-width 和 --input-height 必须同时提供", file=sys.stderr)
        return 2
    models = args.models.expanduser()
    gesture_model = models / "gesture_recognizer.task"
    yunet_model = models / "face_detection_yunet_2023mar.onnx"

    if not gesture_model.is_file():
        print(f"找不到手势模型：{gesture_model}", file=sys.stderr)
        return 2
    if not args.no_face and not yunet_model.is_file():
        print(f"找不到 YuNet 模型：{yunet_model}", file=sys.stderr)
        return 2

    source = camera_source(args.camera)
    capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    if not capture.isOpened():
        print(f"无法打开摄像头：{args.camera}", file=sys.stderr)
        return 2
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    capture.set(cv2.CAP_PROP_FPS, args.fps)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    recognizer = None
    detector = None
    try:
        recognizer = create_gesture_recognizer(gesture_model, args.hands)
        if not args.no_face:
            detector = cv2.FaceDetectorYN.create(
                str(yunet_model), "", (input_width, input_height), 0.7, 0.3, 5000
            )

        print("实时测试已开始，按 Ctrl-C 退出。")
        negotiated_code = int(capture.get(cv2.CAP_PROP_FOURCC))
        negotiated_fourcc = bytes(
            (negotiated_code >> (8 * index)) & 0xFF for index in range(4)
        ).decode("ascii", "replace")
        print(
            f"camera={args.camera} requested={args.width}x{args.height}@{args.fps:g} {args.fourcc} "
            f"negotiated={int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
            f"{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}@"
            f"{capture.get(cv2.CAP_PROP_FPS):g} {negotiated_fourcc} hands={args.hands}"
        )
        print(f"model_input={input_width}x{input_height}")
        print("每秒输出：FPS | faces | hands | gestures")

        started = time.monotonic()
        last_report = started
        window_started = started
        frames = 0
        window_frames = 0
        window_read_failures = 0
        window_latencies: list[float] = []
        last_labels: tuple[str, ...] = ()
        previous_labels: tuple[str, ...] = ()
        window_transitions = 0
        label_counts: collections.Counter[str] = collections.Counter()

        while True:
            ok, frame = capture.read()
            if not ok:
                window_read_failures += 1
                continue

            infer_started = time.perf_counter()
            if frame.shape[1] != input_width or frame.shape[0] != input_height:
                inference_frame = cv2.resize(
                    frame, (input_width, input_height), interpolation=cv2.INTER_AREA
                )
            else:
                inference_frame = frame
            if detector is not None:
                detector.setInputSize((input_width, input_height))
                _, faces = detector.detect(inference_frame)
                face_count = 0 if faces is None else len(faces)
            else:
                face_count = -1

            rgb = cv2.cvtColor(inference_frame, cv2.COLOR_BGR2RGB)
            result = recognizer.recognize(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            )
            labels: list[str] = []
            for gesture_list in result.gestures:
                if gesture_list:
                    label = gesture_list[0].category_name or "None"
                    labels.append(label)
                    if label != "None":
                        label_counts[label] += 1
            last_labels = tuple(labels)
            if last_labels != previous_labels and (last_labels or previous_labels):
                window_transitions += 1
            previous_labels = last_labels
            frames += 1
            window_frames += 1
            window_latencies.append((time.perf_counter() - infer_started) * 1000)

            now = time.monotonic()
            if now - last_report >= 1.0:
                elapsed = now - started
                window_elapsed = now - window_started
                fps = frames / elapsed
                window_fps = window_frames / window_elapsed
                face_text = "-" if face_count < 0 else str(face_count)
                hand_text = str(len(result.hand_landmarks))
                gesture_text = ",".join(last_labels) if last_labels else "None"
                p50 = statistics.median(window_latencies) if window_latencies else 0.0
                p95 = (
                    sorted(window_latencies)[max(0, int(len(window_latencies) * 0.95) - 1)]
                    if window_latencies
                    else 0.0
                )
                print(
                    f"fps_now={window_fps:5.1f} avg={fps:5.1f} | "
                    f"latency_ms={p50:5.1f}/{p95:5.1f} p50/p95 | "
                    f"faces={face_text} | hands={hand_text} | gestures={gesture_text} | "
                    f"switches={window_transitions} read_fail={window_read_failures} | "
                    f"seen={dict(label_counts)}",
                    flush=True,
                )
                last_report = now
                window_started = now
                window_frames = 0
                window_read_failures = 0
                window_latencies.clear()
                window_transitions = 0
    except KeyboardInterrupt:
        print("\n测试结束。")
        return 0
    finally:
        capture.release()
        if recognizer is not None:
            recognizer.close()


if __name__ == "__main__":
    raise SystemExit(main())
