"""Optional One Euro target filter in locally camera-motion-free coordinates."""
import math
import numpy as np


class AdaptiveTargetFilter:
    def __init__(self, min_cutoff=1.5, beta=8.0, error_gain=20.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.error_gain = error_gain
        self.reset()

    def reset(self):
        self.key = None
        self.stamp = None
        self.raw = self.filtered = None
        self.speed = np.zeros(2)
        self.cutoff = self.min_cutoff

    @staticmethod
    def alpha(cutoff, dt):
        return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff * dt))

    def update(self, value, stamp, key, error):
        value = np.asarray(value, dtype=float)
        # Only new observations advance the filter; never feed 25 Hz repeats.
        if self.key == key and self.stamp is not None and stamp == self.stamp:
            return self.filtered.copy()
        if (self.key != key or self.stamp is None or stamp <= self.stamp
                or stamp - self.stamp > .3):
            self.reset()
            self.key, self.stamp = key, stamp
            self.raw = self.filtered = value.copy()
            return value.copy()
        dt = stamp - self.stamp
        derivative = (value - self.raw) / dt
        a = self.alpha(1.0, dt)
        self.speed += a * (derivative - self.speed)
        self.cutoff = (self.min_cutoff + self.beta * float(np.linalg.norm(self.speed))
                       + self.error_gain * float(np.linalg.norm(error)))
        a = self.alpha(self.cutoff, dt)
        self.filtered += a * (value - self.filtered)
        self.raw, self.stamp = value.copy(), stamp
        return self.filtered.copy()
