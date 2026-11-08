"""Tipos de entrada compartilhados (sem dependência de pygame)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from ..link.protocol import RcCommand


class Action(Enum):
    TAKEOFF = auto()
    LAND = auto()
    EMERGENCY = auto()
    SPEED_UP = auto()
    SPEED_DOWN = auto()
    SPEED_SET_0 = auto()
    SPEED_SET_1 = auto()
    SPEED_SET_2 = auto()
    TOGGLE_RECORD = auto()
    SNAPSHOT = auto()
    TOGGLE_DETECT = auto()
    QUIT = auto()


@dataclass(frozen=True)
class ManualInput:
    rc: RcCommand
    active: bool                                  # algum eixo fora da zona morta
    t: float
    device: str
    speed_pct: int
    sticks: tuple[float, float, float, float]     # lr, fb, ud, yaw normalizados (-1..1)
