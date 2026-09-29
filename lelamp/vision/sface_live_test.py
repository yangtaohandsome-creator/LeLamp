#!/usr/bin/env python3
"""LeLamp 独立 SFace 身份识别验证程序。

只读取摄像头并把注册特征保存在实验目录，不连接 LampApp、不保存原始照片。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np


POSES = (
    "正对摄像头，保持自然表情",
    "正对摄像头，轻微抬头",
    "正对摄像头，轻微低头",
    "头部轻轻转向左侧",
    "头部轻轻转向右侧",
    "回到正面，稍微靠近摄像头",
    "回到正面，稍微远离摄像头",
    "正对摄像头，轻微微笑",
    "正对摄像头，恢复自然表情",
    "最后保持正面和自然表情",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LeLamp 独立 SFace 身份识别测试")
    parser.add_argument("mode", choices=("enroll", "verify"), help="注册或实时验证")
    parser.add_argument("--name", help="enroll 时必填；verify 时省略则加载全部身份")
    parser.add_argument("--camera", default="0", help="摄像头编号或路径，默认 0")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--min-face", type=int, default=100, help="最小人脸框宽度，默认 100 像素")
    parser.add_argument("--known-threshold", type=float, default=0.45)
    parser.add_argument("--uncertain-threshold", type=float, default=0.35)
    parser.add_argument("--models", type=Path, default=Path.home() / "lelamp_vision_eval" / "models")
    parser.add_argument("--faces", type=Path, default=Path.home() / "lelamp_vision_eval" / "faces")
    return parser.parse_args()


def safe_name(value: str) -> str:
    name = value.strip()
    if not name or not re.fullmatch(r"[\w\-\u4e00-\u9fff]+", name):
        raise ValueError("名字只能包含中文、字母、数字、下划线或短横线")
    return name


def camera_source(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def normalize(feature: np.ndarray) -> np.ndarray:
    vector = np.asarray(feature, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        raise RuntimeError("SFace 返回了无效特征")
    return vector / norm


class FacePipeline:
    def __init__(self, models: Path, width: int, height: int):
        yunet = models / "face_detection_yunet_2023mar.onnx"
        sface = models / "face_recognition_sface_2021dec.onnx"
        for path in (yunet, sface):
            if not path.is_file():
                raise FileNotFoundError(f"找不到模型：{path}")
        self.detector = cv2.FaceDetectorYN.create(str(yunet), "", (width, height), 0.7, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(sface), "")

    def extract(self, frame: np.ndarray, min_face: int):
        self.detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None, "没有检测到人脸"
        if len(faces) > 1:
            return None, f"检测到 {len(faces)} 张脸，请只保留一人"
        face = faces[0]
        width, height = float(face[2]), float(face[3])
        if width < min_face or height < min_face:
            return None, f"人脸太小（{int(width)}×{int(height)}），请靠近一点"
        started = time.perf_counter()
        aligned = self.recognizer.alignCrop(frame, face)
        feature = normalize(self.recognizer.feature(aligned))
        elapsed_ms = (time.perf_counter() - started) * 1000
        return (feature, face, elapsed_ms), None


def open_camera(args: argparse.Namespace):
    capture = cv2.VideoCapture(camera_source(args.camera), cv2.CAP_V4L2)
    if not capture.isOpened():
        raise RuntimeError(f"无法打开摄像头：{args.camera}")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    for _ in range(8):
        capture.read()
    return capture


def read_valid(capture, pipeline: FacePipeline, min_face: int, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error = "尚未读取到画面"
    while time.monotonic() < deadline:
        ok, frame = capture.read()
        if not ok:
            last_error = "摄像头读取失败"
            continue
        result, error = pipeline.extract(frame, min_face)
        if result is not None:
            return result
        last_error = error or last_error
    raise RuntimeError(last_error)


def enroll(args: argparse.Namespace) -> int:
    if not args.name:
        print("enroll 模式必须提供 --name", file=sys.stderr)
        return 2
    name = safe_name(args.name)
    faces_dir = args.faces.expanduser()
    faces_dir.mkdir(parents=True, exist_ok=True)
    output = faces_dir / f"{name}.npz"
    pipeline = FacePipeline(args.models.expanduser(), args.width, args.height)
    capture = open_camera(args)
    features: list[np.ndarray] = []
    print(f"准备注册：{name}")
    print("每一步调整好姿势后按 Enter；程序会自动等待清晰的单人画面。按 Ctrl-C 取消。")
    try:
        for index, prompt in enumerate(POSES, 1):
            input(f"[{index}/{len(POSES)}] {prompt}，准备好后按 Enter：")
            try:
                feature, face, elapsed_ms = read_valid(capture, pipeline, args.min_face)
            except RuntimeError as exc:
                print(f"  本次未采集：{exc}。请重新调整。")
                feature, face, elapsed_ms = read_valid(capture, pipeline, args.min_face, timeout=12.0)
            features.append(feature)
            print(
                f"  已采集，人脸={int(face[2])}×{int(face[3])}，"
                f"SFace={elapsed_ms:.1f} ms"
            )
    except (KeyboardInterrupt, EOFError):
        print("\n注册已取消，未写入特征文件。")
        return 130
    finally:
        capture.release()

    matrix = np.stack(features).astype(np.float32)
    centroid = normalize(matrix.mean(axis=0))
    similarities = matrix @ centroid
    metadata = {
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "samples": len(features),
        "width": args.width,
        "height": args.height,
        "self_similarity_min": float(similarities.min()),
        "self_similarity_mean": float(similarities.mean()),
    }
    np.savez_compressed(output, centroid=centroid, samples=matrix, metadata=json.dumps(metadata, ensure_ascii=False))
    print(f"注册完成：{output}")
    print(
        f"注册样本内部相似度：min={similarities.min():.3f} "
        f"mean={similarities.mean():.3f} max={similarities.max():.3f}"
    )
    return 0


def load_profiles(directory: Path, selected: str | None):
    profiles: dict[str, np.ndarray] = {}
    candidates = [directory / f"{safe_name(selected)}.npz"] if selected else sorted(directory.glob("*.npz"))
    for path in candidates:
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as data:
            profiles[path.stem] = normalize(data["centroid"])
    if not profiles:
        raise RuntimeError(f"没有可用注册特征：{directory}")
    return profiles


def verify(args: argparse.Namespace) -> int:
    profiles = load_profiles(args.faces.expanduser(), args.name)
    pipeline = FacePipeline(args.models.expanduser(), args.width, args.height)
    capture = open_camera(args)
    print("已加载身份：" + "、".join(profiles))
    print("实时验证已开始，每秒输出一次结果；按 Ctrl-C 退出。")
    last_report = 0.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                continue
            result, error = pipeline.extract(frame, args.min_face)
            now = time.monotonic()
            if now - last_report < 1.0:
                continue
            last_report = now
            if result is None:
                print(f"识别结果：未识别（{error}）", flush=True)
                continue
            feature, face, elapsed_ms = result
            scores = {name: float(feature @ centroid) for name, centroid in profiles.items()}
            best_name, score = max(scores.items(), key=lambda item: item[1])
            if score >= args.known_threshold:
                identity = best_name
                state = "已确认"
            elif score >= args.uncertain_threshold:
                identity = "不确定"
                state = f"最接近 {best_name}"
            else:
                identity = "陌生人"
                state = f"最接近 {best_name}"
            print(
                f"识别结果：{identity} | {state} | cosine={score:.3f} | "
                f"face={int(face[2])}×{int(face[3])} | SFace={elapsed_ms:.1f} ms",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\n验证结束。")
        return 0
    finally:
        capture.release()


def main() -> int:
    args = parse_args()
    if args.uncertain_threshold >= args.known_threshold:
        print("uncertain-threshold 必须小于 known-threshold", file=sys.stderr)
        return 2
    try:
        return enroll(args) if args.mode == "enroll" else verify(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
