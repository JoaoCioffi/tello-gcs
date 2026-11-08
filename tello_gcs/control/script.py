"""Entrada roteirizada (para testes automáticos e demos), sem pygame.

Formato: "1:takeoff, 4:rc 0 40 0 0, 6:rc 0 0 0 0, 7:land, 12:quit"
(tempo em segundos desde o início -> ação ou rc mantido até o próximo rc)
"""
from __future__ import annotations

from dataclasses import dataclass

from ..link.protocol import RC_ZERO, RcCommand
from .types import Action, ManualInput

_NAMES = {
    "takeoff": Action.TAKEOFF, "land": Action.LAND, "emergency": Action.EMERGENCY,
    "quit": Action.QUIT, "snapshot": Action.SNAPSHOT, "record": Action.TOGGLE_RECORD,
}


@dataclass
class _Step:
    t: float
    action: Action | None = None
    rc: RcCommand | None = None


class ScriptedInput:
    def __init__(self, script: str, t0: float) -> None:
        self.t0 = t0
        self.steps: list[_Step] = []
        for part in script.split(","):
            part = part.strip()
            if not part:
                continue
            ts, cmd = part.split(":", 1)
            cmd = cmd.strip()
            if cmd.startswith("rc"):
                vals = [int(v) for v in cmd.split()[1:5]]
                self.steps.append(_Step(float(ts), rc=RcCommand.of(*vals)))
            else:
                self.steps.append(_Step(float(ts), action=_NAMES[cmd]))
        self.steps.sort(key=lambda s: s.t)
        self._next = 0
        self._rc = RC_ZERO

    def pop_actions(self, now: float) -> list[Action]:
        acts = []
        while self._next < len(self.steps) and self.steps[self._next].t <= now - self.t0:
            st = self.steps[self._next]
            self._next += 1
            if st.rc is not None:
                self._rc = st.rc
            if st.action is not None:
                acts.append(st.action)
        return acts

    def poll(self, now: float) -> ManualInput:
        rc = self._rc
        sticks = (rc.lr / 100, rc.fb / 100, rc.ud / 100, rc.yaw / 100)
        return ManualInput(rc, not rc.is_zero(), now, "script", 100, sticks)
