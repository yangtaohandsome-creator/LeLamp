#!/usr/bin/env python3
"""Compare stock and palm-aligned bundled classifiers on still images only."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lelamp.vision.gesture_classifier_experiment import (
    BundledGestureClassifier, palm_aligned_inputs, preprocess_landmarks,
    right_hand_score,
)


def run(bundle: Path, images: list[Path], output: Path | None) -> None:
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    classifier = BundledGestureClassifier(bundle)
    recognizer = vision.GestureRecognizer.create_from_options(
        vision.GestureRecognizerOptions(
            base_options=python.BaseOptions(model_asset_path=str(bundle)),
            running_mode=vision.RunningMode.IMAGE,
            num_hands=2,
        )
    )
    destination = output.open("w", encoding="utf-8") if output else None
    try:
        for path in images:
            image = mp.Image.create_from_file(str(path))
            result = recognizer.recognize(image)
            if not result.hand_landmarks:
                print(json.dumps({"image": str(path), "hands": 0}, ensure_ascii=False))
                continue
            for index, landmarks in enumerate(result.hand_landmarks):
                image_points = tuple(
                    (float(point.x), float(point.y), float(point.z))
                    for point in landmarks
                )
                world_points = tuple(
                    (float(point.x), float(point.y), float(point.z))
                    for point in result.hand_world_landmarks[index]
                )
                side = result.handedness[index][0]
                right_score = right_hand_score(side.category_name, side.score)
                baseline = classifier.classify(
                    preprocess_landmarks(image_points, (image.width, image.height)),
                    preprocess_landmarks(world_points), right_score,
                )
                frontal_hand, frontal_world = palm_aligned_inputs(
                    image_points, world_points
                )
                frontal = classifier.classify(
                    frontal_hand, frontal_world, right_score
                )
                stock = result.gestures[index][0]
                row = {
                    "image": str(path), "hand_index": index,
                    "handedness": side.category_name,
                    "stock": {"label": stock.category_name, "confidence": stock.score},
                    "reproduced": asdict(baseline),
                    "palm_aligned_experiment": asdict(frontal),
                    "stock_reproduced": (
                        stock.category_name == baseline.label
                        and abs(stock.score - baseline.confidence) < 1e-4
                    ),
                }
                line = json.dumps(row, ensure_ascii=False)
                print(line, flush=True)
                if destination is not None:
                    destination.write(line + "\n")
    finally:
        recognizer.close()
        if destination is not None:
            destination.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="已验证的 gesture_recognizer.task")
    parser.add_argument("--output", type=Path, help="可选 JSONL 结果文件")
    parser.add_argument("images", type=Path, nargs="+", help="测试图片；不访问摄像头")
    args = parser.parse_args()
    for path in [args.model, *args.images]:
        if not path.is_file():
            parser.error(f"文件不存在：{path}")
    run(args.model, args.images, args.output)


if __name__ == "__main__":
    main()
