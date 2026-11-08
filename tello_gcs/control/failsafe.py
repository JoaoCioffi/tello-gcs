"""Regras de segurança avaliadas a cada ciclo do loop de controle (função pura + memória mínima)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import SafetyConfig
from ..link.protocol import RC_ZERO, RcCommand, TelloState


@dataclass
class FailsafeDecision:
    rc: RcCommand
    actions: list[str] = field(default_factory=list)   # ex.: ["land"]
    warnings: list[str] = field(default_factory=list)


class Failsafe:
    def __init__(self, cfg: SafetyConfig, link_lost_s: float = 1.0) -> None:
        self.cfg = cfg
        self.link_lost_s = link_lost_s
        self._land_triggered = False

    def evaluate(self, state: Optional[TelloState], state_age_s: float, flying: bool,
                 rc: RcCommand, input_stale: bool = False) -> FailsafeDecision:
        cfg = self.cfg
        d = FailsafeDecision(rc)
        if not flying:
            self._land_triggered = False

        if state is None or state_age_s > self.link_lost_s:
            d.rc = RC_ZERO
            d.warnings.append("LINK PERDIDO: hover")
            return d

        if input_stale:
            d.rc = RC_ZERO
            d.warnings.append("ENTRADA PARADA: hover")

        # bateria
        if state.bat <= cfg.land_battery_pct:
            d.warnings.append(f"BATERIA CRÍTICA {state.bat}%")
            if flying and not self._land_triggered:
                self._land_triggered = True
                d.actions.append("land")
        elif state.bat <= cfg.warn_battery_pct:
            d.warnings.append(f"bateria baixa {state.bat}%")

        # temperatura
        if state.temph >= cfg.temp_critical_c:
            d.warnings.append(f"TEMPERATURA CRÍTICA {state.temph}°C")
        elif state.temph >= cfg.temp_warn_c:
            d.warnings.append(f"temperatura alta {state.temph}°C")

        # teto de altura (h = altura relativa à decolagem)
        if flying and state.h >= cfg.max_height_cm:
            lr, fb, ud, yaw = d.rc.lr, d.rc.fb, d.rc.ud, d.rc.yaw
            ud = -30 if state.h > cfg.max_height_cm + 30 else min(ud, 0)
            d.rc = RcCommand(lr, fb, ud, yaw)
            d.warnings.append(f"TETO {cfg.max_height_cm} cm")
        return d

    def can_takeoff(self, state: Optional[TelloState], state_age_s: float) -> tuple[bool, str]:
        if state is None or state_age_s > self.link_lost_s:
            return False, "sem telemetria"
        if state.bat < self.cfg.takeoff_min_battery_pct:
            return False, f"bateria {state.bat}% < {self.cfg.takeoff_min_battery_pct}%"
        return True, "ok"
