import numpy as np
import pytest

from tracker.aim import MOUNTS, angle_to_dmx16, dmx16_to_angle, local_dir, solve_angles

HUNG = MOUNTS["hung"]
POS = np.array([5.0, 4.0, 5.85])


def beam(R, pan, tilt):
    return R @ local_dir(pan, tilt)


def test_dmx_angle_roundtrip_and_invert():
    for inv in (False, True):
        v = angle_to_dmx16(37.5, 540, inv)
        assert dmx16_to_angle(v, 540, inv) == pytest.approx(37.5, abs=0.01)
    assert angle_to_dmx16(0, 540) == pytest.approx(32768, abs=1)
    assert angle_to_dmx16(999, 540) == 65535  # clamps


def test_straight_down_and_straight_ahead():
    pan, tilt, ok = solve_angles(HUNG, POS, POS * [1, 1, 0], 540, 270)
    assert ok and tilt == pytest.approx(90, abs=1e-6)
    pan, tilt, ok = solve_angles(HUNG, POS, POS + [0, 5, 0], 540, 270)
    assert ok and pan == pytest.approx(0, abs=1e-6) and tilt == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("target", [[9, 12, 0], [1, 2, 1.5], [5, 0, 0], [14, 20, 3], [5, 4.5, 0]])
def test_solution_points_at_target(target):
    pan, tilt, ok = solve_angles(HUNG, POS, np.array(target, float), 540, 270)
    assert ok
    want = np.array(target, float) - POS
    want /= np.linalg.norm(want)
    assert beam(HUNG, pan, tilt) @ want == pytest.approx(1.0, abs=1e-9)


def test_picks_solution_nearest_current_pan():
    # Target behind the head: pan can be +180 or -180. Stay on the side we're already on.
    t = POS + [0, -5, 0]
    pan_pos, _, _ = solve_angles(HUNG, POS, t, 540, 270, current=(150, 0))
    pan_neg, _, _ = solve_angles(HUNG, POS, t, 540, 270, current=(-150, 0))
    assert pan_pos == pytest.approx(180, abs=1e-6)
    assert pan_neg == pytest.approx(-180, abs=1e-6)


def test_uses_flipped_solution_when_it_is_closer():
    # Slightly behind and low: the flipped solution keeps pan on the same side.
    t = np.array([5.0, 3.0, 0.0])
    a = solve_angles(HUNG, POS, t, 540, 270, current=(0, 0))
    b = solve_angles(HUNG, POS, t, 540, 270, current=(180, 130))
    assert a[2] and b[2]
    assert abs(a[0] - b[0]) > 90  # two different but equally valid ways of aiming


def test_unreachable_is_clamped_and_flagged():
    pan, tilt, ok = solve_angles(HUNG, POS, POS + [0, 5, 20], 540, 90, current=(0, 0))
    assert not ok and abs(tilt) <= 45 + 1e-9


def test_target_at_fixture_returns_none():
    assert solve_angles(HUNG, POS, POS.copy(), 540, 270) is None
