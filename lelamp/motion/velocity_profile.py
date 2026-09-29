"""Independent-axis, analytic jerk-limited transitions to a requested velocity.

Each call replans from the command velocity/acceleration (not measured motor
velocity). Three phases: ramp acceleration, optional constant acceleration,
then ramp to zero acceleration. Integrating each phase also gives position.
Safety stops/position limits may override continuity outside this helper.
"""
import math
import numpy as np


def _axis(velocity, acceleration, target, max_acceleration, jerk, dt):
    # Velocity reached if we bring acceleration to zero with maximum jerk.
    stopping_delta = acceleration * abs(acceleration) / (2 * jerk)
    delta = target - velocity
    direction = 1.0 if delta >= stopping_delta else -1.0
    a0 = direction * acceleration
    distance = direction * delta
    peak = math.sqrt(max(0.0, jerk * distance + .5 * a0 * a0))
    peak = min(peak, max_acceleration)
    up = max(0.0, (peak - a0) / jerk)
    plateau = (max(0.0, (distance - (2 * peak * peak - a0 * a0) / (2 * jerk)) / peak)
               if peak > 1e-12 else 0.0)
    phases = ((up, direction * jerk), (plateau, 0.0), (peak / jerk, -direction * jerk))
    remaining, displacement = dt, 0.0
    for duration, rate in phases:
        t = min(remaining, duration)
        displacement += velocity * t + .5 * acceleration * t*t + rate * t*t*t / 6
        velocity += acceleration * t + .5 * rate * t*t
        acceleration += rate * t
        remaining -= t
        if remaining <= 1e-12:
            return displacement, velocity, acceleration
    # Completed profile: continue at the requested constant speed.
    displacement += target * remaining
    return displacement, target, 0.0


def velocity_step(velocity, acceleration, target, max_acceleration, jerk, dt):
    """Return (integrated displacement, next velocity, next acceleration)."""
    values = np.asarray([_axis(v, a, w, limit, j, dt)
                         for v, a, w, limit, j in zip(
                             velocity, acceleration, target, max_acceleration, jerk)])
    return values[:, 0], values[:, 1], values[:, 2]
