"""Árbitro de controle: decide quem comanda o drone a cada ciclo.

Prioridade: emergência/failsafe (fora daqui) > MANUAL > AUTÔNOMO.
Qualquer deflexão de stick toma o controle na hora e mantém a autonomia pausada por
`override_hold_s` depois que o piloto solta o stick.
"""
from __future__ import annotations

from typing import Optional

from ..link.protocol import RC_ZERO, RcCommand


class Arbiter:
    def __init__(self, override_hold_s: float = 1.0) -> None:
        self.override_hold_s = override_hold_s
        self._last_manual = float("-inf")

    def decide(self, manual: RcCommand, manual_active: bool, auto: Optional[RcCommand], now: float) -> tuple[RcCommand, str]:
        if manual_active:
            self._last_manual = now
            return manual, "MANUAL"
        if auto is not None and now - self._last_manual > self.override_hold_s:
            return auto, "AUTO"
        return RC_ZERO, "MANUAL"

    @property
    def last_manual_t(self) -> float:
        return self._last_manual
