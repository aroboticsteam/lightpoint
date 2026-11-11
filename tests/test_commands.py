import shutil
from pathlib import Path

import pytest

from tracker.commands import CommandError, apply
from tracker.config import load_config
from tracker.engine import Engine

CONFIG = Path(__file__).parent.parent / "config" / "fixtures.example.json"


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / "fixtures.json"
    shutil.copy(CONFIG, path)
    return Engine(load_config(path))


def test_claim_and_release(engine):
    apply(engine, {"type": "claim", "op": "p1", "fid": "R1", "on": True})
    assert engine.owner["R1"] == "p1"
    apply(engine, {"type": "claim", "op": "p1", "fid": "R1", "on": False})
    assert engine.owner["R1"] is None


def test_cannot_take_a_held_light(engine):
    apply(engine, {"type": "claim", "op": "p1", "fid": "R1"})
    with pytest.raises(CommandError, match="held by Person 1"):
        apply(engine, {"type": "claim", "op": "p2", "fid": "R1"})
    with pytest.raises(CommandError):
        apply(engine, {"type": "claim", "op": "p2", "fid": "R1", "on": False})  # nor let it go


def test_select_all_free_skips_held_ones(engine):
    apply(engine, {"type": "claim", "op": "p2", "fid": "R3"})
    apply(engine, {"type": "claim_all", "op": "p1", "on": True})
    assert engine.owner["R3"] == "p2"
    assert sum(o == "p1" for o in engine.owner.values()) == len(engine.fixtures) - 1
    apply(engine, {"type": "claim_all", "op": "p1", "on": False})
    assert all(o in (None, "p2") for o in engine.owner.values()) and engine.owner["R3"] == "p2"


def test_mode_only_for_own_lights(engine):
    apply(engine, {"type": "claim", "op": "p1", "fid": "R1"})
    apply(engine, {"type": "mode", "op": "p1", "mode": "full", "ids": ["R1"]})
    assert engine.state["R1"].mode == "full"
    with pytest.raises(CommandError, match="not held"):
        apply(engine, {"type": "mode", "op": "p2", "mode": "off", "ids": ["R1"]})
    with pytest.raises(CommandError, match="mode must be"):
        apply(engine, {"type": "mode", "op": "p1", "mode": "bogus", "ids": ["R1"]})


def test_target_validation(engine):
    apply(engine, {"type": "target", "op": "p1", "x": 1, "y": 2, "z": 0})
    assert list(engine.operators["p1"]["target"]) == [1, 2, 0]
    apply(engine, {"type": "target", "op": "p1", "clear": True})
    assert engine.operators["p1"]["target"] is None
    for bad in ({"x": "a", "y": 1, "z": 0}, {"x": 1}, {"x": float("nan"), "y": 1, "z": 0}):
        with pytest.raises(CommandError):
            apply(engine, {"type": "target", "op": "p1", **bad})


def test_overview_overrides(engine):
    apply(engine, {"type": "claim", "op": "p1", "fid": "R1"})
    apply(engine, {"type": "assign", "fid": "R1", "op": "p2"})
    assert engine.owner["R1"] == "p2"
    apply(engine, {"type": "mode_all", "mode": "off"})
    assert engine.state["R1"].mode == "off"


def test_unknown_things(engine):
    for msg in ({"type": "nope"}, {"type": "claim", "op": "zz", "fid": "R1"}, {"type": "claim", "op": "p1", "fid": "ZZ"}, []):
        with pytest.raises(CommandError):
            apply(engine, msg)


def test_dynamic_command(engine):
    apply(engine, {"type": "dynamic", "op": "p1", "on": True, "count": 3, "prepare": 2})
    o = engine.operators["p1"]
    assert o["dynamic"] and o["count"] == 3 and o["prepare"] == 2
    apply(engine, {"type": "dynamic", "op": "p1", "count": 2})
    assert o["count"] == 2 and o["dynamic"]  # only what is sent changes
    apply(engine, {"type": "dynamic", "op": "p1", "on": False})
    assert not o["dynamic"]
    for bad in ({"count": 0}, {"count": 99}, {"count": "4"}, {"count": True}, {"prepare": -1}, {"prepare": 1.5}):
        with pytest.raises(CommandError):
            apply(engine, {"type": "dynamic", "op": "p1", **bad})
    with pytest.raises(CommandError, match="unknown operator"):
        apply(engine, {"type": "dynamic", "op": "nobody", "on": True})


def test_reach_is_read_from_the_config_and_validated(tmp_path):
    import json

    from tracker.config import load_config

    cfg = json.loads(CONFIG.read_text())
    cfg["fixtures"][0]["reach"] = 1.35
    p = tmp_path / "f.json"
    p.write_text(json.dumps(cfg))
    assert load_config(p).fixtures[0].reach == 1.35 and load_config(p).fixtures[1].reach == 1.0
    cfg["fixtures"][0]["reach"] = 0
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="reach"):
        load_config(p)


def test_dynamic_back_min_command(engine):
    apply(engine, {"type": "dynamic", "op": "p1", "back_min": 1})
    assert engine.operators["p1"]["back_min"] == 1
    for bad in ({"back_min": -1}, {"back_min": 99}, {"back_min": "1"}, {"back_min": True}):
        with pytest.raises(CommandError):
            apply(engine, {"type": "dynamic", "op": "p1", **bad})


def test_dynamic_front_min_command(engine):
    apply(engine, {"type": "dynamic", "op": "p1", "front_min": 2})
    assert engine.operators["p1"]["front_min"] == 2
    for bad in ({"front_min": -1}, {"front_min": 99}, {"front_min": "1"}, {"front_min": False}):
        with pytest.raises(CommandError):
            apply(engine, {"type": "dynamic", "op": "p1", **bad})
