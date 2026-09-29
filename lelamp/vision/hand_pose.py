"""Camera-angle independent features from MediaPipe's hand world landmarks.

This module only describes hand geometry. It does not assign gesture labels or
send control events; labels need real examples from the installed camera.
"""
from __future__ import annotations

import math

from .types import Point3



def _subtract(first: Point3, second: Point3) -> Point3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _dot(first: Point3, second: Point3) -> float:
    return sum(a * b for a, b in zip(first, second))


def _length(point: Point3) -> float:
    return math.sqrt(_dot(point, point))


def _unit(point: Point3) -> Point3:
    length = _length(point)
    if length < 1e-6:
        raise ValueError("手掌关键点重合，无法建立局部坐标系")
    return (point[0] / length, point[1] / length, point[2] / length)


def _cross(first: Point3, second: Point3) -> Point3:
    ax, ay, az = first
    bx, by, bz = second
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def normalize_world_landmarks(
    landmarks: tuple[Point3, ...], handedness: str | None = None
) -> tuple[Point3, ...]:
    """Put 21 world points in a wrist-centered, palm-aligned, unit-size frame.

    X points from index MCP to pinky MCP; Y points from wrist towards middle
    MCP; Z is their normal. Left hands are mirrored across local Z so the
    resulting points use the same handedness as a right hand.
    """
    if len(landmarks) != 21 or not all(
        len(point) == 3 and all(math.isfinite(value) for value in point)
        for point in landmarks
    ):
        raise ValueError("手部世界坐标必须包含21个有限的三维关键点")

    origin = landmarks[0]
    width = _subtract(landmarks[17], landmarks[5])
    scale = _length(width)
    if scale < 1e-6:
        raise ValueError("掌骨跨度过小，无法归一化")
    x_axis = _unit(width)
    forward = _subtract(landmarks[9], origin)
    forward_x = _dot(forward, x_axis)
    y_axis = _unit(_subtract(forward, (
        x_axis[0] * forward_x,
        x_axis[1] * forward_x,
        x_axis[2] * forward_x,
    )))
    z_axis = _cross(x_axis, y_axis)
    z_sign = -1.0 if handedness == "Left" else 1.0
    return tuple(
        (
            _dot(relative, x_axis) / scale,
            _dot(relative, y_axis) / scale,
            z_sign * _dot(relative, z_axis) / scale,
        )
        for relative in (_subtract(point, origin) for point in landmarks)
    )


def finger_pip_angles(landmarks: tuple[Point3, ...]) -> tuple[float, ...]:
    """Return thumb IP and four finger PIP angles, in degrees.

    Straight fingers approach 180 degrees; bent fingers have smaller angles.
    """
    if len(landmarks) != 21:
        raise ValueError("需要21个手部关键点")
    angles = []
    for before, joint, after in ((2, 3, 4), (5, 6, 7), (9, 10, 11),
                                 (13, 14, 15), (17, 18, 19)):
        first = _subtract(landmarks[before], landmarks[joint])
        second = _subtract(landmarks[after], landmarks[joint])
        denominator = _length(first) * _length(second)
        if denominator < 1e-12:
            raise ValueError("手指关键点重合，无法计算关节角度")
        cosine = max(-1.0, min(1.0, _dot(first, second) / denominator))
        angles.append(math.degrees(math.acos(cosine)))
    return tuple(angles)
