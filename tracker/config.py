"""Load and save config/fixtures.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .aim import MOUNTS
from .fixtures import PROFILES, Fixture


@dataclass
class Config:
    path: Path
    listen_host: str
    listen_port: int
    in_universe: int
    out_host: str
    out_port: int
    out_universe: int
    tick_hz: float
    hall: dict
    fixtures: list[Fixture] = field(default_factory=list)
    operators: list[dict] = field(default_factory=list)  # {"id", "name", "fixtures": [ids]}
    disconnect_release_s: float = 15.0  # a person's lights go back to the board this long after their page closes
    dynamic_hysteresis_m: float = 0.75  # dynamic mode: a light must be this much closer to take a place from a current one


def magicq_to_world(p) -> np.ndarray:
    """MagicQ patch (x, y = height, z = depth) -> world (x, y = depth, z = height)."""
    return np.array([p[0], p[2], p[1]], float)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = json.loads(path.read_text())
    inp, out = raw["input"], raw["output"]
    fixtures = []
    for f in raw["fixtures"]:
        prof = PROFILES[f["profile"]]
        R = np.array(f["orientation"], float) if "orientation" in f else MOUNTS[f.get("mount", "hung")]
        fixtures.append(
            Fixture(
                id=f["id"],
                profile=prof,
                address=f["address"],
                pos=magicq_to_world(f["pos_magicq"]),
                R=R,
                pan_range=f.get("pan_range", prof.pan_range),
                tilt_range=f.get("tilt_range", prof.tilt_range),
                pan_invert=f.get("pan_invert", False),
                tilt_invert=f.get("tilt_invert", False),
                max_slew=f.get("max_slew_deg_s", 0.0),
                calibrated="orientation" in f,
                reach=float(f.get("reach", 1.0)),
                group=str(f.get("group", "")),
            )
        )
    cfg = Config(
        path=path,
        listen_host=inp.get("host", "0.0.0.0"),
        listen_port=inp.get("port", 6454),
        in_universe=inp["universe"],
        out_host=out["host"],
        out_port=out.get("port", 6454),
        out_universe=out["universe"],
        tick_hz=raw.get("tick_hz", 40),
        hall=raw.get("hall", {"width": 17.65, "depth": 34.19}),
        fixtures=fixtures,
        operators=raw.get("operators", []),
        disconnect_release_s=raw.get("disconnect_release_s", 15.0),
        dynamic_hysteresis_m=raw.get("dynamic_hysteresis_m", 0.75),
    )
    for f in fixtures:
        if not f.reach > 0:
            raise ValueError(f"fixture {f.id}: reach must be greater than 0")
    known, seen = {f.id for f in fixtures}, set()
    for o in cfg.operators:
        for fid in o["fixtures"]:
            if fid not in known:
                raise ValueError(f"operator {o['id']} references unknown fixture {fid}")
            if fid in seen:
                raise ValueError(f"fixture {fid} is assigned to more than one operator")
            seen.add(fid)
    loopback = cfg.out_host.startswith("127.") or cfg.out_host.endswith(".255")
    if cfg.in_universe == cfg.out_universe and cfg.listen_port == cfg.out_port and loopback:
        raise ValueError(
            "input and output share a universe/port on a broadcast or loopback address: "
            "the proxy would read its own output. Use different universes or a unicast host."
        )
    return cfg


def save_orientation(path: str | Path, fixture_id: str, R: np.ndarray, rms_deg: float, n: int) -> None:
    path = Path(path)
    raw = json.loads(path.read_text())
    for f in raw["fixtures"]:
        if f["id"] == fixture_id:
            f["orientation"] = [[round(float(v), 6) for v in row] for row in R]
            f["calibration"] = {"rms_deg": round(rms_deg, 3), "samples": n}
            break
    else:
        raise KeyError(fixture_id)
    path.write_text(json.dumps(raw, indent=2) + "\n")
