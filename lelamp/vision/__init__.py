"""LeLamp vision functionality."""

from .config import VisionConfig, load_vision_config
from .controller import VisionController
from .target import FaceTargetManager
from .hand_target import HandTargetManager
from .tracking import NestedTrackingSession
from .types import (
    FaceObservation,
    FramePacket,
    GestureEvent,
    HandTrackingCandidate,
    HandObservation,
    TrackingTarget,
    TrackingDirective,
    VisionSnapshot,
)

__all__ = [
    "FaceObservation",
    "FaceTargetManager",
    "FramePacket",
    "GestureEvent",
    "HandTargetManager",
    "HandTrackingCandidate",
    "HandObservation",
    "TrackingTarget",
    "TrackingDirective",
    "NestedTrackingSession",
    "VisionConfig",
    "VisionController",
    "VisionSnapshot",
    "load_vision_config",
]
