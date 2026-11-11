import shutil
from pathlib import Path

import numpy as np
import pytest

from tracker.aim import angle_to_dmx16, solve_angles
from tracker.config import load_config
from tracker.engine import Engine

CONFIG = Path(__file__).parent.parent / "config" / "fixtures.example.json"


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / "fixtures.json"
    shutil.copy(CONFIG, path)
    return Engine(load_config(path))


def put16(eng, fid, v_pan, v_tilt):
    fx = eng.fixtures[fid]
    p = fx.profile
    eng.board[fx.slot(p.pan)], eng.board[fx.slot(p.pan_fine)] = v_pan >> 8, v_pan & 255
    eng.board[fx.slot(p.tilt)], eng.board[fx.slot(p.tilt_fine)] = v_tilt >> 8, v_tilt & 255


def get16(frame, fx, coarse, fine):
    return (frame[fx.slot(coarse)] << 8) | frame[fx.slot(fine)]


def run(eng, seconds, dt=0.025):
    frame = None
    for _ in range(int(seconds / dt)):
        frame = eng.tick(dt)
    return frame


def test_nothing_is_held_to_begin_with(engine):
    assert all(o is None for o in engine.owner.values())


def test_passthrough_is_identical(engine):
    engine.on_board_frame(bytes(range(256)) * 2)
    assert engine.tick(0.025) == bytes(engine.board)


# -- selecting lights ---------------------------------------------------------------------------
def test_claim_starts_following_and_blocks_others(engine):
    engine.claim("p1", "R1")
    assert engine.owner["R1"] == "p1" and engine.state["R1"].mode == "pan_tilt"
    with pytest.raises(ValueError, match="held by Person 1"):
        engine.claim("p2", "R1")
    engine.claim("p1", "R1")  # claiming your own is a no-op


def test_unclaim_frees_and_releases(engine):
    engine.claim("p1", "R1")
    with pytest.raises(ValueError):
        engine.unclaim("p2", "R1")  # not yours to let go
    engine.unclaim("p1", "R1")
    assert engine.owner["R1"] is None and engine.state["R1"].mode == "off"
    engine.claim("p2", "R1")  # now free for someone else


def test_free_head_cannot_be_driven(engine):
    with pytest.raises(ValueError):
        engine.set_mode("R1", "pan_tilt")


def test_admin_assign_overrides_a_holder(engine):
    engine.claim("p1", "R1")
    engine.assign("R1", "p2")
    assert engine.owner["R1"] == "p2"
    engine.assign("R1", None)
    assert engine.owner["R1"] is None and engine.state["R1"].mode == "off"


# -- output -------------------------------------------------------------------------------------
def test_only_overrides_pan_tilt_speed(engine):
    engine.on_board_frame(bytes([77]) * 512)
    engine.claim("p1", "R1")
    engine.set_target("p1", [8, 14, 0])
    frame = run(engine, 6)
    fx = engine.fixtures["R1"]
    p = fx.profile
    changed = {i for i in range(512) if frame[i] != engine.board[i]}
    allowed = {fx.slot(c) for c in (p.pan, p.pan_fine, p.tilt, p.tilt_fine, p.speed)}
    assert changed and changed <= allowed
    assert frame[fx.slot(p.control)] == 77  # control channel is never touched
    assert frame[fx.slot(p.speed)] == 0


def test_people_move_their_own_heads_independently(engine):
    engine.on_board_frame(bytes(512))
    for op, fids in (("p1", ("R1", "R2")), ("p2", ("R3", "R4"))):
        for fid in fids:
            engine.claim(op, fid)
    engine.set_target("p1", [3, 10, 0])
    engine.set_target("p2", [13, 18, 0])
    frame = run(engine, 8)
    pans = {fid: get16(frame, engine.fixtures[fid], 1, 2) for fid in ("R1", "R2", "R3", "R4")}
    assert pans["R1"] != pans["R3"] and pans["R2"] != pans["R4"]
    before = {fid: engine.state[fid].pan for fid in ("R1", "R2")}
    engine.set_target("p2", [1, 2, 0])  # person 2 moves; person 1's heads don't
    run(engine, 2)
    assert {fid: engine.state[fid].pan for fid in ("R1", "R2")} == before


def test_slew_limit_and_start_from_board(engine):
    fx = engine.fixtures["S1"]
    fx.max_slew = 60.0  # an explicit cap; the default is no cap
    put16(engine, "S1", angle_to_dmx16(-100, fx.pan_range, fx.pan_invert), angle_to_dmx16(0, fx.tilt_range, fx.tilt_invert))
    engine.claim("p3", "S1")
    engine.set_target("p3", [14, 3, 0])
    engine.tick(0.025)
    st = engine.state["S1"]
    assert st.pan == pytest.approx(-100, abs=2)  # engaged where the board had it
    prev = st.pan
    for _ in range(40):
        engine.tick(0.025)
        assert abs(st.pan - prev) <= fx.max_slew * 0.025 + 1e-9
        prev = st.pan


def test_release_eases_back_to_board_then_passes_through(engine):
    put16(engine, "R3", 20000, 30000)
    engine.on_board_frame(bytes(engine.board))
    engine.claim("p2", "R3")
    engine.set_target("p2", [2, 18, 0])
    run(engine, 6)
    engine.unclaim("p2", "R3")  # lets go while it is away from the board's position
    assert engine.state["R3"].phase == "tracking"
    run(engine, 8)
    assert engine.state["R3"].phase == "passthrough"
    assert engine.tick(0.025) == bytes(engine.board)


def test_release_returns_to_the_boards_latest_values_not_the_ones_at_claim_time(engine):
    """The board keeps sending while a person holds a light; letting go lands on what it sent last."""
    fx = engine.fixtures["R3"]
    put16(engine, "R3", 20000, 30000)
    engine.on_board_frame(bytes(engine.board))
    engine.claim("p2", "R3")
    engine.set_mode("R3", "full")  # also takes over dimmer / colour
    engine.set_target("p2", [2, 18, 0])
    run(engine, 4)

    # the board changes everything for this head while the tracker is holding it
    put16(engine, "R3", 50000, 10000)
    engine.board[fx.slot(6)] = 123  # dimmer
    engine.board[fx.slot(8)] = 45  # red
    engine.on_board_frame(bytes(engine.board))
    assert engine.tick(0.025)[fx.slot(6)] == 255  # still held: our dimmer, not the board's

    engine.unclaim("p2", "R3")
    frame = engine.tick(0.025)
    assert frame[fx.slot(6)] == 123 and frame[fx.slot(8)] == 45  # light channels snap straight back
    frame = run(engine, 10)  # pan/tilt ease back at the slew limit, then hand over exactly
    assert engine.state["R3"].phase == "passthrough"
    assert frame == bytes(engine.board)
    assert get16(frame, fx, 1, 2) == 50000 and get16(frame, fx, 3, 4) == 10000


def test_release_follows_a_board_that_keeps_moving_while_it_eases(engine):
    fx = engine.fixtures["R3"]
    put16(engine, "R3", 20000, 30000)
    engine.claim("p2", "R3")
    engine.set_target("p2", [2, 18, 0])
    run(engine, 4)
    engine.unclaim("p2", "R3")
    put16(engine, "R3", 40000, 33000)  # board moves it mid-ease
    run(engine, 10)
    assert engine.tick(0.025) == bytes(engine.board)


def test_full_takeover_sets_light_channels(engine):
    engine.on_board_frame(bytes(512))
    engine.claim("p1", "R2")
    engine.set_target("p1", [8, 14, 0])
    engine.set_mode("R2", "full")
    frame = run(engine, 1)
    fx = engine.fixtures["R2"]
    assert frame[fx.slot(6)] == 255  # dimmer
    assert frame[fx.slot(17)] == 0  # control stays untouched


def test_calibration_updates_orientation(engine):
    import json

    fx = engine.fixtures["S1"]
    for pt in [(3, 12, 0), (9, 15, 0), (6, 8, 0)]:
        pan, tilt, _ = solve_angles(fx.R, fx.pos, np.array(pt, float), fx.pan_range, fx.tilt_range)
        put16(engine, "S1", angle_to_dmx16(pan, fx.pan_range, fx.pan_invert), angle_to_dmx16(tilt, fx.tilt_range, fx.tilt_invert))
        engine.record_sample("S1", pt)
    rms = engine.solve("S1")
    assert rms < 0.05 and fx.calibrated
    assert "orientation" in next(f for f in json.loads(engine.cfg.path.read_text())["fixtures"] if f["id"] == "S1")


def test_default_follows_target_instantly(engine):
    fx = engine.fixtures["S1"]
    assert fx.max_slew == 0.0
    put16(engine, "S1", angle_to_dmx16(-100, fx.pan_range, fx.pan_invert), angle_to_dmx16(0, fx.tilt_range, fx.tilt_invert))
    engine.claim("p3", "S1")
    engine.set_target("p3", [14, 3, 0])
    engine.tick(0.025)  # engages from the board's position
    engine.tick(0.025)
    first = engine.state["S1"].pan
    engine.set_target("p3", [2, 20, 0])
    engine.tick(0.025)  # one tick later the head is already at the new aim, not 1.5 deg closer
    assert abs(engine.state["S1"].pan - first) > 5
