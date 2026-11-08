"""Funções puras de condicionamento de sinal dos sticks (testáveis sem pygame)."""
from __future__ import annotations

import math


def apply_deadzone(v: float, deadzone: float) -> float:
    """Zona morta com reescala: |v|<=dz -> 0, e o restante é reescalado para [0, 1]."""
    a = abs(v)
    if a <= deadzone:
        return 0.0
    return math.copysign(min(1.0, (a - deadzone) / (1.0 - deadzone)), v)


def apply_expo(v: float, expo: float) -> float:
    """Curva expo clássica de rádio: mais precisão perto do centro."""
    return (1.0 - expo) * v + expo * v * v * v


def shape_axis(v: float, deadzone: float, expo: float) -> float:
    return apply_expo(apply_deadzone(max(-1.0, min(1.0, v)), deadzone), expo)
