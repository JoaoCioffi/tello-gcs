"""Mapeamento do controle -> eixos do drone (função pura, testável sem pygame).

Layout escolhido pelo piloto (PS5):
  stick esquerdo  Y: frente/trás (pitch)    X: direita/esquerda (roll)
  R2 / L2         sobe / desce (proporcional ao gatilho)
  R1 / L1         yaw direita / esquerda (taxa fixa = yaw_button_level)
  stick direito   livre
"""
from __future__ import annotations

from dataclasses import dataclass

from .shaping import apply_deadzone, shape_axis


@dataclass(frozen=True)
class PadSample:
    lx: float          # -1..1 (direita +)
    ly: float          # -1..1 (convenção SDL: para BAIXO é +)
    rx: float
    ry: float
    lt: float          # 0..1 (L2)
    rt: float          # 0..1 (R2)
    l1: bool
    r1: bool


def map_pad(p: PadSample, deadzone: float, expo: float, trigger_deadzone: float,
            yaw_button_level: float) -> tuple[float, float, float, float]:
    """Retorna (lr, fb, ud, yaw) normalizados em [-1, 1]."""
    lr = shape_axis(p.lx, deadzone, expo)
    fb = shape_axis(-p.ly, deadzone, expo)
    lt = apply_deadzone(max(0.0, min(1.0, p.lt)), trigger_deadzone)
    rt = apply_deadzone(max(0.0, min(1.0, p.rt)), trigger_deadzone)
    ud = rt - lt
    yaw = (float(p.r1) - float(p.l1)) * yaw_button_level
    return lr, fb, ud, yaw
