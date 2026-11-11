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


# -- dynamic mode ---------------------------------------------------------------------------------
def _dyn_setup(engine, fids=("R1", "R2", "R3", "R4", "S1", "S2"), count=3, prepare=1):
    for fid in fids:
        engine.claim("p1", fid)
    engine.set_dynamic("p1", on=True, count=count, prepare=prepare)


def _roles(engine, fids):
    return {fid: engine.state[fid].role for fid in fids}


def test_dynamic_follows_only_the_closest_and_parks_the_rest_down(engine):
    _dyn_setup(engine)
    engine.set_target("p1", [3.8, 4.4, 0])  # right under R1; S1, S2 then R2 are next nearest
    run(engine, 0.5)
    roles = _roles(engine, ("R1", "R2", "R3", "R4", "S1", "S2"))
    assert [f for f, r in roles.items() if r == "active"] == ["R1", "S1", "S2"]
    assert [f for f, r in roles.items() if r == "armed"] == ["R2"]
    assert {f for f, r in roles.items() if r == "idle"} == {"R3", "R4"}
    # an idle head looks straight down: its beam direction has no horizontal part
    fx, st = engine.fixtures["R4"], engine.state["R4"]
    from tracker.aim import local_dir
    beam = fx.R @ local_dir(st.pan, st.tilt)
    assert beam[2] == pytest.approx(-1, abs=1e-3)


def test_dynamic_set_follows_the_pointer(engine):
    _dyn_setup(engine, count=2, prepare=0)
    engine.set_target("p1", [3.8, 4.4, 0])
    run(engine, 0.2)
    assert {f for f, r in _roles(engine, engine.fixtures).items() if r == "active"} == {"R1", "S1"}
    engine.set_target("p1", [13.5, 4.0, 0])  # moved across the hall
    run(engine, 0.2)
    active = {f for f, r in _roles(engine, engine.fixtures).items() if r == "active"}
    assert active <= {"R3", "R4", "S3", "S4"} and len(active) == 2
    assert engine.state["R1"].role == "idle"


def test_dynamic_lights_follow_on_and_prepared_or_parked_ones_are_off(engine):
    _dyn_setup(engine, fids=("R1", "R2", "R3"), count=1, prepare=1)  # modes stay "pan_tilt" (Follow)
    dimmer = 6  # Rogue dimmer channel
    for fid in ("R1", "R2", "R3"):
        fx = engine.fixtures[fid]
        engine.board[fx.slot(dimmer)] = 200  # the board has them all lit
    engine.set_target("p1", [3.8, 4.4, 0])
    frame = run(engine, 0.3)
    assert engine.state["R1"].role == "active" and engine.state["R2"].role == "armed" and engine.state["R3"].role == "idle"
    fx1, fx2, fx3 = (engine.fixtures[f] for f in ("R1", "R2", "R3"))
    assert frame[fx1.slot(dimmer)] == 255  # following: on
    assert frame[fx2.slot(dimmer)] == 0  # pre-aiming: off
    assert frame[fx3.slot(dimmer)] == 0  # parked pointing down: off
    # the prepared head is still aimed at the pointer
    sol = solve_angles(fx2.R, fx2.pos, np.array([3.8, 4.4, 0]), fx2.pan_range, fx2.tilt_range)
    assert engine.state["R2"].pan == pytest.approx(sol[0], abs=0.5)
    # and it comes on the moment it is promoted
    engine.set_target("p1", [7.5, 4.0, 0])
    frame = run(engine, 0.1)
    assert engine.state["R2"].role == "active" and frame[fx2.slot(dimmer)] == 255
    assert frame[fx1.slot(dimmer)] in (0, 255) and engine.state["R1"].role != "active" and frame[fx1.slot(dimmer)] == 0


def test_dynamic_without_a_target_everything_is_dark_and_pointing_down(engine):
    _dyn_setup(engine, fids=("S1", "P1"))
    for fid, ch in (("S1", 5), ("P1", 5)):
        engine.board[engine.fixtures[fid].slot(ch)] = 180
    frame = run(engine, 0.2)
    for fid in ("S1", "P1"):
        fx = engine.fixtures[fid]
        assert engine.state[fid].role == "idle"
        assert frame[fx.slot(5)] == 0 and frame[fx.slot(6)] == 0  # 16-bit dimmer off


def test_dynamic_hysteresis_stops_flicker_between_similar_distances(engine):
    _dyn_setup(engine, fids=("R2", "R3"), count=1, prepare=0)
    mid = (engine.fixtures["R2"].pos + engine.fixtures["R3"].pos) / 2
    engine.set_target("p1", mid - [0.2, 0, 0])  # R2 slightly closer
    run(engine, 0.1)
    assert engine.state["R2"].role == "active"
    engine.set_target("p1", mid + [0.2, 0, 0])  # R3 now slightly closer, but not by the margin
    run(engine, 0.1)
    assert engine.state["R2"].role == "active"
    engine.set_target("p1", mid + [2.0, 0, 0])  # clearly R3's now
    run(engine, 0.1)
    assert engine.state["R3"].role == "active" and engine.state["R2"].role == "idle"


def test_dynamic_without_a_target_points_everything_down(engine):
    _dyn_setup(engine, fids=("R1", "R2"))
    run(engine, 0.2)
    assert set(_roles(engine, ("R1", "R2")).values()) == {"idle"}


def test_dynamic_off_goes_back_to_all_following_and_held_heads_stay_out(engine):
    _dyn_setup(engine, fids=("R1", "R2", "R3"), count=1, prepare=0)
    engine.set_mode("R3", "off")  # holding to the board: not part of the pool
    engine.set_target("p1", [3.8, 4.4, 0])
    run(engine, 0.2)
    assert engine.state["R3"].role == "" and engine.state["R2"].role == "idle"
    engine.set_dynamic("p1", on=False)
    run(engine, 0.2)
    assert engine.state["R2"].role == "" and engine.state["R1"].role == ""
    sol = solve_angles(engine.fixtures["R2"].R, engine.fixtures["R2"].pos, np.array([3.8, 4.4, 0]),
                       engine.fixtures["R2"].pan_range, engine.fixtures["R2"].tilt_range)
    assert engine.state["R2"].pan == pytest.approx(sol[0], abs=0.5)


def test_dynamic_count_is_validated_and_clamped_to_the_pool(engine):
    with pytest.raises(ValueError):
        engine.set_dynamic("p1", count=0)
    _dyn_setup(engine, fids=("R1", "R2"), count=8, prepare=2)  # more than the pool holds
    engine.set_target("p1", [3.8, 4.4, 0])
    run(engine, 0.1)
    assert set(_roles(engine, ("R1", "R2")).values()) == {"active"}


def test_dynamic_reach_makes_a_far_light_count_as_closer(engine):
    _dyn_setup(engine, fids=("R2", "R3"), count=1, prepare=0)
    mid = (engine.fixtures["R2"].pos + engine.fixtures["R3"].pos) / 2
    engine.set_target("p1", mid - [1.0, 0, 0])  # R2 is clearly nearer
    run(engine, 0.1)
    assert engine.state["R2"].role == "active"
    engine.set_dynamic("p1", on=False)
    engine.set_dynamic("p1", on=True)
    engine.fixtures["R3"].reach = 10.0  # R3 (2.3 m off) now counts as 0.23 m, closer than R2 (0.3 m)
    run(engine, 0.1)
    assert engine.state["R3"].role == "active" and engine.state["R2"].role == "idle"


# -- dynamic mode: keep spots for the back bar -----------------------------------------------------
def _back_setup(engine, count=2, back_min=1):
    for fid in ("P1", "P2", "R1", "R2", "S1"):
        engine.claim("p1", fid)
    for fid in ("P1", "P2"):
        engine.fixtures[fid].group = "back"
    engine.set_dynamic("p1", on=True, count=count, prepare=0, back_min=back_min)


def test_back_bar_gets_at_least_one_of_the_spots_even_when_front_lights_are_closer(engine):
    _back_setup(engine, count=2, back_min=1)
    engine.set_target("p1", [4.5, 4.2, 0])  # far from the back bar (about 15 m), right under R1/S1
    run(engine, 0.2)
    roles = _roles(engine, ("P1", "P2", "R1", "R2", "S1"))
    active = {f for f, r in roles.items() if r == "active"}
    assert len(active) == 2 and len(active & {"P1", "P2"}) == 1
    assert "P1" in active  # the closest back light of the two (P1 is at x=5.9)
    assert "R1" in active or "S1" in active  # and the closest front one keeps the other spot
    assert {f for f, r in roles.items() if r == "idle"} >= {"R2"}


def test_back_bar_option_off_picks_by_distance_only(engine):
    _back_setup(engine, count=2, back_min=0)
    engine.set_target("p1", [4.5, 4.2, 0])
    run(engine, 0.2)
    assert {f for f, r in _roles(engine, ("P1", "P2", "R1", "R2", "S1")).items() if r == "active"} == {"R1", "S1"}


def test_back_bar_reservation_is_capped_by_the_spots_and_by_how_many_back_lights_there_are(engine):
    _back_setup(engine, count=2, back_min=4)  # asks for more than the two spots / two back lights
    engine.set_target("p1", [4.5, 4.2, 0])
    run(engine, 0.2)
    assert {f for f, r in _roles(engine, ("P1", "P2", "R1", "R2", "S1")).items() if r == "active"} == {"P1", "P2"}
    engine.set_dynamic("p1", count=3, back_min=3)  # three spots, two back lights: both back + the closest front one
    run(engine, 0.2)
    active = {f for f, r in _roles(engine, ("P1", "P2", "R1", "R2", "S1")).items() if r == "active"}
    assert active >= {"P1", "P2"} and len(active) == 3 and active & {"R1", "S1"}


def test_back_bar_reservation_changes_nothing_when_a_back_light_is_already_close(engine):
    _back_setup(engine, count=2, back_min=1)
    engine.set_target("p1", [5.9, 18.5, 0])  # under the back bar
    run(engine, 0.2)
    assert {f for f, r in _roles(engine, ("P1", "P2", "R1", "R2", "S1")).items() if r == "active"} == {"P1", "P2"}


# -- dynamic mode: keep spots for the front bar (and back bar together) ----------------------------
def _bar_setup(engine, count, back_min=0, front_min=0):
    for fid in ("P1", "P2", "R1", "R2", "S1"):
        engine.claim("p1", fid)
    for fid in ("P1", "P2"):
        engine.fixtures[fid].group = "back"
    for fid in ("R1", "R2", "S1"):
        engine.fixtures[fid].group = "front"
    engine.set_dynamic("p1", on=True, count=count, prepare=0, back_min=back_min, front_min=front_min)


def _active(engine):
    return {f for f, r in _roles(engine, ("P1", "P2", "R1", "R2", "S1")).items() if r == "active"}


def test_front_bar_gets_at_least_one_spot_even_when_the_back_bar_is_closer(engine):
    _bar_setup(engine, count=2, front_min=1)
    engine.set_target("p1", [5.9, 18.5, 0])  # under the back bar, about 15 m from the front one
    run(engine, 0.2)
    active = _active(engine)
    assert len(active) == 2 and len(active & {"R1", "R2", "S1"}) == 1 and active & {"P1", "P2"}


def test_front_and_back_minimums_are_both_kept(engine):
    _bar_setup(engine, count=3, back_min=1, front_min=2)
    engine.set_target("p1", [5.9, 18.5, 0])  # back bar is far nearer: without the minimums it would take all three
    run(engine, 0.2)
    active = _active(engine)
    assert len(active) == 3 and len(active & {"P1", "P2"}) == 1 and len(active & {"R1", "R2", "S1"}) == 2


def test_front_option_off_picks_by_distance_and_the_two_minimums_share_the_spots(engine):
    _bar_setup(engine, count=2, front_min=0)
    engine.set_target("p1", [5.9, 18.5, 0])
    run(engine, 0.2)
    assert _active(engine) == {"P1", "P2"}
    engine.set_dynamic("p1", back_min=2, front_min=2)  # 4 asked for, 2 spots: back first, front gets what is left
    run(engine, 0.2)
    assert _active(engine) == {"P1", "P2"}
