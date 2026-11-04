"""Pan/tilt maths.

World frame: X across the hall, Y depth from the front wall, Z up (metres).
Fixture frame: Z is the pan axis, Y is the beam direction at pan = tilt = 0.
`R` maps fixture-frame vectors into the world frame (columns = fixture axes).
Angles are measured from mid-range, so DMX 32768 is pan = tilt = 0 for every
head; any real mounting/zero offset is absorbed into `R` by calibration.
"""
from __future__ import annotations

import math

import numpy as np

# Where the board may have left the head: hung from a truss, pan 0 facing +Y.
MOUNTS = {
    "hung": np.array([[-1.0, 0, 0], [0, 1, 0], [0, 0, -1]]),
    "upright": np.eye(3),
}


def local_dir(pan_deg: float, tilt_deg: float) -> np.ndarray:
    p, t = math.radians(pan_deg), math.radians(tilt_deg)
    return np.array([-math.sin(p) * math.cos(t), math.cos(p) * math.cos(t), math.sin(t)])


def angle_to_dmx16(angle: float, rng: float, invert: bool = False) -> int:
    frac = angle / rng + 0.5
    if invert:
        frac = 1.0 - frac
    return round(min(max(frac, 0.0), 1.0) * 65535)


def dmx16_to_angle(value: int, rng: float, invert: bool = False) -> float:
    frac = value / 65535.0
    if invert:
        frac = 1.0 - frac
    return (frac - 0.5) * rng


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def solve_angles(
    R: np.ndarray,
    pos: np.ndarray,
    target: np.ndarray,
    pan_range: float,
    tilt_range: float,
    current: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, float, bool] | None:
    """Return (pan, tilt, reachable) in degrees, or None if target == position.

    A direction usually has several (pan, tilt) solutions: pan +/- 360 within a
    540/630 degree range, and the flipped pan+180 / tilt mirrored pair when
    tilt exceeds 90. The one nearest `current` is chosen so heads never spin
    the long way round. If none fit the limits, the nearest clamp is returned
    with reachable = False.
    """
    d = np.asarray(target, float) - np.asarray(pos, float)
    n = np.linalg.norm(d)
    if n < 1e-6:
        return None
    l = R.T @ (d / n)
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, l[2]))))
    pan = math.degrees(math.atan2(-l[0], l[1]))
    half_p, half_t = pan_range / 2, tilt_range / 2

    pairs = [(pan, tilt), (pan + 180.0, _wrap(180.0 - tilt))]
    valid = []
    for pp, tt in pairs:
        pp = _wrap(pp)
        if abs(tt) > half_t:
            continue
        for k in range(-2, 3):
            cand = pp + 360.0 * k
            if abs(cand) <= half_p + 1e-9:
                valid.append((cand, tt))
    if valid:
        best = min(valid, key=lambda c: abs(c[0] - current[0]) + abs(c[1] - current[1]))
        return best[0], best[1], True
    pp, tt = _wrap(pan), tilt
    return max(-half_p, min(half_p, pp)), max(-half_t, min(half_t, tt)), False
