"""Legacy constructor retained; playback now shares app/Motion implementation."""
from .motors_service import MotorsService


class AnimationService(MotorsService):
    def __init__(self, port, lamp_id, fps=30, duration=5.0, idle_recording="idle"):
        super().__init__(port, lamp_id, fps)
        # Retained for callers; motion.conf now owns transition timing.
        self.duration = duration
        self.idle_recording = idle_recording
