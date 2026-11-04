"""Fixture profiles (from the manufacturer DMX charts) and fixture instances."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Profile:
    """Channel numbers are 1-based offsets from the fixture's DMX address."""

    name: str
    footprint: int
    pan: int
    pan_fine: int
    tilt: int
    tilt_fine: int
    control: int  # reset / invert / load-defaults channel: always passed through
    pan_range: float  # default degrees; override per fixture if the menu differs
    tilt_range: float
    speed: int | None = None  # pan/tilt speed channel, if the mode has one (0 = fastest)
    takeover: dict[int, int] = field(default_factory=dict)  # "full" mode: light on, white


PROFILES: dict[str, Profile] = {
    # Chauvet Rogue R2X Wash, 17ch (DMX chart Rev 5, manual Rev 9)
    "rogue_r2x_17": Profile(
        "Rogue R2X Wash 17ch", 17, 1, 2, 3, 4, control=17,
        pan_range=540, tilt_range=230, speed=5,
        takeover={6: 255, 7: 20, 8: 255, 9: 255, 10: 255, 11: 255},  # dimmer, strobe "on", RGBW
    ),
    # Cameo Azor S2, 28CH Extended (control table D002972)
    "azor_s2_28": Profile(
        "Azor S2 28ch", 28, 1, 2, 3, 4, control=28,
        pan_range=540, tilt_range=270,
        takeover={5: 255, 6: 255, 7: 0, 8: 0, 10: 0, 12: 0},  # dimmer 16-bit, strobe open, CMY 0
    ),
    # Cameo Azor SP2, 43CH Extended (control table D002980a)
    "azor_sp2_43": Profile(
        "Azor SP2 43ch", 43, 1, 2, 3, 4, control=43,
        pan_range=540, tilt_range=270,
        takeover={5: 255, 6: 255, 7: 0, 8: 0, 10: 0, 12: 0},
    ),
}


@dataclass
class Fixture:
    id: str
    profile: Profile
    address: int  # 1-based DMX start address
    pos: np.ndarray  # world frame (x, y depth, z up), metres
    R: np.ndarray  # fixture -> world rotation
    pan_range: float
    tilt_range: float
    pan_invert: bool = False
    tilt_invert: bool = False
    max_slew: float = 60.0  # deg/s the software lets pan/tilt move
    calibrated: bool = False

    def slot(self, channel: int) -> int:
        """0-based index into a 512-byte DMX frame for a profile channel."""
        return self.address - 1 + channel - 1
