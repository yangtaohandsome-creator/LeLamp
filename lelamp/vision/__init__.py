"""LeLamp vision functionality."""

from .config import VisionConfig, load_vision_config
from .controller import VisionController
from .target import FaceTargetManager
from .types import (
    FaceObservation,
    FramePacket,
    GestureEvent,
    HandObservation,
    TrackingTarget,
    VisionSnapshot,
)

__all__ = [
    "FaceObservation",
    "FaceTargetManager",
    "FramePacket",
    "GestureEvent",
    "HandObservation",
    "TrackingTarget",
    "VisionConfig",
    "VisionController",
    "VisionSnapshot",
    "load_vision_config",
]
