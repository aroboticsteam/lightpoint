"""Solve a head's mounting orientation from aim-at-known-point samples."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .aim import dmx16_to_angle, local_dir


@dataclass
class Sample:
    point: tuple[float, float, float]  # world position the head was aimed at
    pan16: int
    tilt16: int


def solve_orientation(
    samples: list[Sample],
    pos: np.ndarray,
    pan_range: float,
    tilt_range: float,
    pan_invert: bool = False,
    tilt_invert: bool = False,
) -> tuple[np.ndarray, float]:
    """Kabsch fit: rotation R (fixture -> world) and RMS error in degrees.

    Needs at least two aim points that are not in the same direction from the
    head; three or four spread around the hall is better.
    """
    if len(samples) < 2:
        raise ValueError("need at least 2 samples")
    L, W = [], []
    for s in samples:
        pan = dmx16_to_angle(s.pan16, pan_range, pan_invert)
        tilt = dmx16_to_angle(s.tilt16, tilt_range, tilt_invert)
        L.append(local_dir(pan, tilt))
        w = np.asarray(s.point, float) - pos
        W.append(w / np.linalg.norm(w))
    L, W = np.array(L), np.array(W)
    H = L.T @ W
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T)) or 1.0
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    err = [math.degrees(math.acos(max(-1.0, min(1.0, float((R @ l) @ w))))) for l, w in zip(L, W)]
    return R, math.sqrt(sum(e * e for e in err) / len(err))
