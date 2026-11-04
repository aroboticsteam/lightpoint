import math

import numpy as np
import pytest

from tracker.aim import angle_to_dmx16, solve_angles
from tracker.calibrate import Sample, solve_orientation


def rot_z(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def rot_x(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1.0, 0, 0], [0, c, -s], [0, s, c]])


def make_samples(R_true, pos, points, pr=540, tr=270):
    out = []
    for pt in points:
        pan, tilt, ok = solve_angles(R_true, pos, np.array(pt, float), pr, tr)
        assert ok
        out.append(Sample(tuple(pt), angle_to_dmx16(pan, pr), angle_to_dmx16(tilt, tr)))
    return out


def test_recovers_unknown_mounting():
    pos = np.array([6.6, 5.0, 4.2])
    R_true = rot_z(33) @ np.diag([1.0, -1.0, -1.0]) @ rot_x(7)  # hung, twisted, slightly tilted
    pts = [(3, 12, 0), (10, 14, 0), (8, 9, 0), (5, 16, 0)]
    R, rms = solve_orientation(make_samples(R_true, pos, pts), pos, 540, 270)
    assert rms < 0.05  # 16-bit quantisation only
    # the recovered orientation aims the same way as the truth at fresh points
    for t in [(1, 18, 0), (12, 8, 1.5)]:
        a = solve_angles(R, pos, np.array(t, float), 540, 270)
        b = solve_angles(R_true, pos, np.array(t, float), 540, 270)
        assert a[0] == pytest.approx(b[0], abs=0.1) and a[1] == pytest.approx(b[1], abs=0.1)


def test_needs_two_samples():
    with pytest.raises(ValueError):
        solve_orientation([Sample((1, 2, 0), 100, 100)], np.zeros(3), 540, 270)
