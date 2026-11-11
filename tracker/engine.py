"""The merge engine: board frame in, board frame with tracked heads overridden out."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .aim import angle_to_dmx16, dmx16_to_angle, solve_angles
from .calibrate import Sample, solve_orientation
from .config import Config, save_orientation
from .fixtures import Fixture

MODES = ("off", "pan_tilt", "full")


@dataclass
class State:
    mode: str = "off"
    phase: str = "passthrough"  # passthrough | tracking | releasing
    pan: float = 0.0
    tilt: float = 0.0
    reachable: bool = True
    role: str = ""  # dynamic mode only: active (lit and following) | armed (pre-aimed, may be next) | idle (pointing down)


DYN_DEFAULT_COUNT = 4  # dynamic mode: how many of a person's lights follow the pointer
DYN_DEFAULT_PREPARE = 1  # ...and how many more, next in line, are pre-aimed so they are ready when needed
DYN_MAX = 16
DOWN = np.array([0.0, 0.0, -1.0])
RELEASE_SLEW_DEG_S = 90.0  # a head handed back to the board eases there at this rate instead of snapping


def _slew(cur: float, dest: float, max_step: float) -> float:
    return cur + max(-max_step, min(max_step, dest - cur))


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.board = bytearray(512)
        self.fixtures = {f.id: f for f in cfg.fixtures}
        self.state = {f.id: State() for f in cfg.fixtures}
        self.samples: dict[str, list[Sample]] = {f.id: [] for f in cfg.fixtures}
        self.operators: dict[str, dict] = {
            o["id"]: {
                "name": o["name"],
                "target": None,
                "dynamic": bool(o.get("dynamic", False)),
                "count": int(o.get("dynamic_count", DYN_DEFAULT_COUNT)),
                "prepare": int(o.get("dynamic_prepare", DYN_DEFAULT_PREPARE)),
                "active": [],  # fixture ids currently in the active set (kept for hysteresis)
            }
            for o in cfg.operators
        }
        self.owner: dict[str, str | None] = {f.id: None for f in cfg.fixtures}
        for o in cfg.operators:
            for fid in o["fixtures"]:
                self.owner[fid] = o["id"]
        self.last_input: float | None = None
        self.frames_in = 0

    # -- input ---------------------------------------------------------
    def on_board_frame(self, data: bytes) -> None:
        self.board[: len(data)] = data[:512]
        if len(data) < 512:
            self.board[len(data) :] = bytes(512 - len(data))
        self.last_input = time.monotonic()
        self.frames_in += 1

    # -- operators -----------------------------------------------------
    def set_target(self, op: str, xyz) -> None:
        self.operators[op]["target"] = None if xyz is None else np.asarray(xyz, float)

    def target_for(self, fid: str) -> np.ndarray | None:
        op = self.owner[fid]
        return self.operators[op]["target"] if op else None

    def operator_fixtures(self, op: str) -> list[str]:
        return [fid for fid in self.fixtures if self.owner[fid] == op]

    def set_mode(self, fid: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("mode must be off, pan_tilt or full")
        if mode != "off" and self.owner[fid] is None:
            raise ValueError(f"{fid} is not assigned to an operator")
        self.state[fid].mode = mode

    def set_dynamic(self, op: str, on: bool | None = None, count: int | None = None, prepare: int | None = None) -> None:
        """Dynamic mode: of the lights I hold, only the `count` closest to my pointer follow it, the next `prepare`
        are pre-aimed, and the rest point straight down. The choice is re-made as the pointer moves."""
        o = self.operators[op]
        if count is not None:
            if not 1 <= count <= DYN_MAX:
                raise ValueError(f"count must be 1 to {DYN_MAX}")
            o["count"] = count
        if prepare is not None:
            if not 0 <= prepare <= DYN_MAX:
                raise ValueError(f"prepare must be 0 to {DYN_MAX}")
            o["prepare"] = prepare
        if on is not None:
            o["dynamic"] = bool(on)
            if not on:
                o["active"] = []

    def _plan_dynamic(self) -> None:
        """Give every light of a dynamic operator its role for this tick."""
        for st in self.state.values():
            st.role = ""
        hyst = self.cfg.dynamic_hysteresis_m
        for op, o in self.operators.items():
            if not o["dynamic"]:
                continue
            pool = [fid for fid in self.fixtures if self.owner[fid] == op and self.state[fid].mode != "off"]
            target = o["target"]
            if target is None:  # nothing to follow: everything points down
                o["active"] = []
                for fid in pool:
                    self.state[fid].role = "idle"
                continue
            prev = set(o["active"])
            # members of the current set get a head start, so two similar distances do not make a light flicker in and out
            score = {
                fid: float(np.linalg.norm(self.fixtures[fid].pos - target)) / self.fixtures[fid].reach - (hyst if fid in prev else 0.0)
                for fid in pool
            }
            order = sorted(pool, key=score.__getitem__)
            n, p = o["count"], o["prepare"]
            o["active"] = order[:n]
            for i, fid in enumerate(order):
                self.state[fid].role = "active" if i < n else "armed" if i < n + p else "idle"

    def claim(self, op: str, fid: str) -> None:
        """An operator selects a free head; it starts following their target."""
        holder = self.owner[fid]
        if holder == op:
            return
        if holder is not None:
            raise ValueError(f"{fid} is held by {self.operators[holder]['name']}")
        self.owner[fid] = op
        self.state[fid].mode = "pan_tilt"

    def unclaim(self, op: str, fid: str) -> None:
        """An operator lets a head go: it eases back to the board and becomes free."""
        if self.owner[fid] != op:
            raise ValueError(f"{fid} is not held by {self.operators[op]['name']}")
        self.state[fid].mode = "off"
        self.owner[fid] = None

    def assign(self, fid: str, op: str | None) -> None:
        """Overview override: move a head to any operator (or free it), even if held."""
        if op is not None and op not in self.operators:
            raise KeyError(op)
        self.state[fid].mode = "pan_tilt" if op else "off"
        self.owner[fid] = op

    # -- helpers -------------------------------------------------------
    def _read16(self, fx: Fixture, coarse: int, fine: int) -> int:
        return (self.board[fx.slot(coarse)] << 8) | self.board[fx.slot(fine)]

    def board_angles(self, fx: Fixture) -> tuple[float, float]:
        p = fx.profile
        pan = dmx16_to_angle(self._read16(fx, p.pan, p.pan_fine), fx.pan_range, fx.pan_invert)
        tilt = dmx16_to_angle(self._read16(fx, p.tilt, p.tilt_fine), fx.tilt_range, fx.tilt_invert)
        return pan, tilt

    @staticmethod
    def _put16(out: bytearray, fx: Fixture, coarse: int, fine: int, value: int) -> None:
        out[fx.slot(coarse)] = value >> 8
        out[fx.slot(fine)] = value & 0xFF

    # -- output --------------------------------------------------------
    def tick(self, dt: float) -> bytes:
        out = bytearray(self.board)
        self._plan_dynamic()
        for fid, fx in self.fixtures.items():
            self._step(fx, self.state[fid], dt, out)
        return bytes(out)

    def _step(self, fx: Fixture, st: State, dt: float, out: bytearray) -> None:
        prof = fx.profile
        if st.mode == "off":
            if st.phase == "tracking":
                st.phase = "releasing"
        elif st.phase == "passthrough":
            st.phase = "tracking"
            st.pan, st.tilt = self.board_angles(fx)  # start from where the board has it
        elif st.phase == "releasing":
            st.phase = "tracking"

        if st.phase == "passthrough":
            return

        if st.phase == "releasing":
            step = RELEASE_SLEW_DEG_S * dt
            dest = self.board_angles(fx)
            st.pan, st.tilt = _slew(st.pan, dest[0], step), _slew(st.tilt, dest[1], step)
            if abs(st.pan - dest[0]) < 0.1 and abs(st.tilt - dest[1]) < 0.1:
                st.phase = "passthrough"
                return
        else:
            sol = None
            target = fx.pos + DOWN if st.role == "idle" else self.target_for(fx.id)
            if target is not None:
                sol = solve_angles(fx.R, fx.pos, target, fx.pan_range, fx.tilt_range, (st.pan, st.tilt))
            if sol is not None:
                if fx.max_slew > 0:
                    step = fx.max_slew * dt
                    st.pan, st.tilt = _slew(st.pan, sol[0], step), _slew(st.tilt, sol[1], step)
                else:  # no cap: the head goes straight to where the pointer is
                    st.pan, st.tilt = sol[0], sol[1]
                st.reachable = sol[2]
            # Dynamic mode: lights that follow are on, lights that are pre-aiming or parked pointing down are off.
            if st.role in ("armed", "idle"):
                for ch, v in prof.blackout.items():
                    out[fx.slot(ch)] = v
            elif st.role == "active" or st.mode == "full":
                for ch, v in prof.takeover.items():
                    out[fx.slot(ch)] = v

        self._put16(out, fx, prof.pan, prof.pan_fine, angle_to_dmx16(st.pan, fx.pan_range, fx.pan_invert))
        self._put16(out, fx, prof.tilt, prof.tilt_fine, angle_to_dmx16(st.tilt, fx.tilt_range, fx.tilt_invert))
        if prof.speed is not None:
            out[fx.slot(prof.speed)] = 0  # fastest: smoothing is done here

    # -- calibration ---------------------------------------------------
    def record_sample(self, fid: str, point) -> int:
        """Record the head's current board pan/tilt as aimed at a known world point."""
        fx = self.fixtures[fid]
        p = fx.profile
        self.samples[fid].append(
            Sample(tuple(point), self._read16(fx, p.pan, p.pan_fine), self._read16(fx, p.tilt, p.tilt_fine))
        )
        return len(self.samples[fid])

    def clear_samples(self, fid: str) -> None:
        self.samples[fid].clear()

    def solve(self, fid: str) -> float:
        fx = self.fixtures[fid]
        R, rms = solve_orientation(
            self.samples[fid], fx.pos, fx.pan_range, fx.tilt_range, fx.pan_invert, fx.tilt_invert
        )
        fx.R, fx.calibrated = R, True
        save_orientation(self.cfg.path, fid, R, rms, len(self.samples[fid]))
        return rms

    # -- status --------------------------------------------------------
    def snapshot(self) -> dict:
        # whole seconds, and no frame counter, so an idle rig produces an unchanging snapshot
        age = None if self.last_input is None else int(time.monotonic() - self.last_input)
        return {
            "input": {"age_s": age},
            "hall": self.cfg.hall,
            "operators": [
                {
                    "id": o,
                    "name": v["name"],
                    "target": None if v["target"] is None else [round(float(x), 3) for x in v["target"]],
                    "dynamic": v["dynamic"],
                    "count": v["count"],
                    "prepare": v["prepare"],
                    "fixtures": self.operator_fixtures(o),
                }
                for o, v in self.operators.items()
            ],
            "fixtures": [
                {
                    "id": fid,
                    "profile": fx.profile.name,
                    "owner": self.owner[fid],
                    "pos": [round(float(v), 3) for v in fx.pos],
                    "mode": st.mode,
                    "phase": st.phase,
                    "role": st.role,
                    "pan": round(st.pan, 1),
                    "tilt": round(st.tilt, 1),
                    "reachable": st.reachable,
                    "calibrated": fx.calibrated,
                    "samples": len(self.samples[fid]),
                }
                for fid, fx in self.fixtures.items()
                for st in [self.state[fid]]
            ],
        }
