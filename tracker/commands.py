"""One command vocabulary, used by the websocket and by POST /api/command.

Every command is a JSON object with a `type`. There is no access control: the page says
who it is (`op`), and the engine only enforces who holds which head.
"""
from __future__ import annotations

import math

from .engine import Engine


class CommandError(Exception):
    pass


def _op(engine: Engine, msg: dict) -> str:
    op = msg.get("op")
    if op not in engine.operators:
        raise CommandError(f"unknown operator {op!r}")
    return op


def _fid(engine: Engine, msg: dict) -> str:
    fid = msg.get("fid")
    if fid not in engine.fixtures:
        raise CommandError(f"unknown fixture {fid!r}")
    return fid


def _xyz(msg: dict) -> list[float]:
    try:
        xyz = [float(msg["x"]), float(msg["y"]), float(msg["z"])]
    except (KeyError, TypeError, ValueError):
        raise CommandError("expected numeric x, y, z")
    if not all(math.isfinite(v) for v in xyz):
        raise CommandError("x, y, z must be finite")
    return xyz


def apply(engine: Engine, msg: dict) -> dict | None:
    """Run one command. Returns an optional reply for the sender; raises CommandError."""
    if not isinstance(msg, dict):
        raise CommandError("command must be a JSON object")
    kind = msg.get("type")
    try:
        if kind == "target":  # {op, x, y, z} or {op, clear: true}
            op = _op(engine, msg)
            engine.set_target(op, None if msg.get("clear") else _xyz(msg))

        elif kind == "claim":  # {op, fid, on}: select / deselect one head
            op, fid = _op(engine, msg), _fid(engine, msg)
            (engine.claim if msg.get("on", True) else engine.unclaim)(op, fid)

        elif kind == "claim_all":  # {op, on}: every free head / every head I hold
            op = _op(engine, msg)
            for fid in list(engine.fixtures):
                if msg.get("on", True) and engine.owner[fid] is None:
                    engine.claim(op, fid)
                elif not msg.get("on", True) and engine.owner[fid] == op:
                    engine.unclaim(op, fid)

        elif kind == "mode":  # {op, mode, ids?}: off | pan_tilt | full, for heads I hold
            op = _op(engine, msg)
            ids = msg.get("ids") or engine.operator_fixtures(op)
            for fid in ids:
                if engine.owner.get(fid) != op:
                    raise CommandError(f"{fid} is not held by {engine.operators[op]['name']}")
            for fid in ids:
                engine.set_mode(fid, msg.get("mode"))

        elif kind == "dynamic":  # {op, on?, count?, prepare?, back_min?}: closest-N mode for my lights
            op = _op(engine, msg)
            for key in ("count", "prepare", "back_min"):
                if key in msg and (isinstance(msg[key], bool) or not isinstance(msg[key], int)):
                    raise CommandError(f"{key} must be a whole number")
            engine.set_dynamic(op, msg.get("on"), msg.get("count"), msg.get("prepare"), msg.get("back_min"))

        elif kind == "assign":  # {fid, op|null}: overview override
            engine.assign(_fid(engine, msg), msg.get("op") or None)

        elif kind == "mode_all":  # {mode}: overview override, e.g. release everything
            mode = msg.get("mode")
            for fid in engine.fixtures:
                if mode == "off" or engine.owner[fid] is not None:
                    engine.set_mode(fid, mode)

        elif kind == "cal":  # {action: sample|clear|solve, fid, x, y, z}
            fid, action = _fid(engine, msg), msg.get("action")
            if action == "sample":
                engine.record_sample(fid, _xyz(msg))
            elif action == "clear":
                engine.clear_samples(fid)
            elif action == "solve":
                return {"type": "solved", "fid": fid, "rms_deg": round(engine.solve(fid), 2)}
            else:
                raise CommandError(f"unknown calibration action {action!r}")

        else:
            raise CommandError(f"unknown command {kind!r}")
    except CommandError:
        raise
    except (KeyError, ValueError) as e:
        raise CommandError(str(e).strip("'\""))
    return None
