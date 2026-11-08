"""Controlador de voo: loop de `rc` em frequência fixa + máquina de estados de voo.

- O loop roda numa thread própria a `rc_hz` (30 Hz), independente da UI/percepção.
  Se a UI travar, a entrada fica "velha" (> input_stale_s) e o loop manda hover.
- Comandos longos (takeoff/land) rodam numa thread worker para não bloquear o loop.
- Emergência é enviada na hora, da própria thread que pediu.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from enum import Enum
from typing import Callable, Optional

from ..bus import RateMeter
from ..config import ControlConfig, SafetyConfig
from ..link.driver import TelloLink
from ..link.protocol import RC_ZERO, RcCommand
from .arbiter import Arbiter
from .failsafe import Failsafe
from .types import Action, ManualInput

log = logging.getLogger(__name__)


class Phase(Enum):
    DISCONNECTED = "DESCONECTADO"
    LANDED = "NO CHÃO"
    TAKING_OFF = "DECOLANDO"
    FLYING = "VOANDO"
    LANDING = "POUSANDO"


class FlightController:
    def __init__(self, link: TelloLink, ctrl: ControlConfig, safety: SafetyConfig,
                 on_event: Optional[Callable[[float, str], None]] = None) -> None:
        self.link = link
        self.ctrl = ctrl
        self.on_event = on_event
        self.phase = Phase.LANDED if link.connected else Phase.DISCONNECTED
        self.arbiter = Arbiter(ctrl.override_hold_s)
        self.failsafe = Failsafe(safety, link.cfg.link_lost_s)
        self.rc_rate = RateMeter()
        self.last_rc: RcCommand = RC_ZERO
        self.source = "MANUAL"
        self.warnings: list[str] = []
        self.events: deque[tuple[float, str]] = deque(maxlen=50)
        self._manual: Optional[ManualInput] = None
        self._auto: Optional[RcCommand] = None
        self._lock = threading.Lock()
        self._jobs: "queue.Queue[str]" = queue.Queue()
        self._ground_since: float | None = None
        self._air_since: float | None = None
        self._threads: list[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        self._running = True
        for fn, name in ((self._rc_loop, "rc-loop"), (self._worker, "flight-worker")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=1.0)
        self._threads.clear()

    def set_manual(self, inp: ManualInput) -> None:
        with self._lock:
            self._manual = inp

    def set_auto(self, rc: Optional[RcCommand]) -> None:
        """Gancho para a autonomia (Fase 3). None = sem comando autônomo."""
        with self._lock:
            self._auto = rc

    def request(self, action: Action) -> None:
        if action == Action.EMERGENCY:
            self.emergency()
        elif action == Action.TAKEOFF:
            self._jobs.put("takeoff")
        elif action == Action.LAND:
            self._jobs.put("land")

    def emergency(self) -> None:
        self.link.emergency()
        self._set_phase(Phase.LANDED)
        self.event("EMERGÊNCIA: motores cortados")

    @property
    def flying(self) -> bool:
        return self.phase in (Phase.FLYING, Phase.TAKING_OFF, Phase.LANDING)

    def event(self, text: str) -> None:
        t = time.monotonic()
        self.events.append((t, text))
        log.info("[voo] %s", text)
        if self.on_event:
            try:
                self.on_event(t, text)
            except Exception:
                log.exception("on_event falhou")

    def wait_landed(self, timeout: float) -> bool:
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if self.phase in (Phase.LANDED, Phase.DISCONNECTED) and self._jobs.empty():
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------ internos
    def _set_phase(self, phase: Phase) -> None:
        if phase != self.phase:
            self._ground_since = self._air_since = None
            log.info("fase: %s -> %s", self.phase.value, phase.value)
            self.phase = phase

    def _worker(self) -> None:
        while self._running:
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if job == "takeoff":
                    self._takeoff()
                elif job == "land":
                    self._land()
            except Exception:
                log.exception("job %s falhou", job)

    def _takeoff(self) -> None:
        if self.phase != Phase.LANDED:
            self.event(f"decolagem ignorada (fase {self.phase.value})")
            return
        t_wait = time.monotonic() + 1.5  # logo após conectar a 1ª telemetria pode não ter chegado
        while not self.link.link_ok and time.monotonic() < t_wait:
            time.sleep(0.05)
        st, _, _ = self.link.state.get()
        ok, why = self.failsafe.can_takeoff(st, self.link.state_age_s)
        if not ok:
            self.event(f"decolagem BLOQUEADA: {why}")
            return
        self._set_phase(Phase.TAKING_OFF)
        self.event("decolando")
        resp = self.link.send("takeoff", timeout=self.link.cfg.takeoff_timeout_s)
        st, _, _ = self.link.state.get()
        if resp == "ok":
            self._set_phase(Phase.FLYING)
            self.event("no ar")
        elif st is not None and st.h > 20:
            self._set_phase(Phase.FLYING)
            self.event(f"no ar (sem confirmação: {resp!r})")
        else:
            self._set_phase(Phase.LANDED)
            self.event(f"falha na decolagem: {resp!r}")

    def _land(self) -> None:
        if self.phase not in (Phase.FLYING, Phase.TAKING_OFF):
            return
        self._set_phase(Phase.LANDING)
        self.event("pousando")
        resp = self.link.send("land", timeout=self.link.cfg.land_timeout_s)
        st, _, _ = self.link.state.get()
        if resp == "ok" or (st is not None and st.h <= 10):
            self._set_phase(Phase.LANDED)
            self.event("pousado")
        else:
            self._set_phase(Phase.FLYING)
            self.event(f"pouso não confirmado: {resp!r}")

    def _autodetect_phase(self, now: float) -> None:
        """Corrige a fase a partir da telemetria (ex.: auto-pouso do próprio Tello)."""
        st, _, _ = self.link.state.get()
        if st is None or not self.link.link_ok:
            return
        on_ground = st.h <= 5 and (not st.tof_valid or st.tof <= 20)
        if self.phase == Phase.FLYING:
            if on_ground:
                self._ground_since = self._ground_since or now
                if now - self._ground_since > 3.0:
                    self._set_phase(Phase.LANDED)
                    self.event("pouso detectado pela telemetria")
            else:
                self._ground_since = None
        elif self.phase == Phase.LANDED:
            # exige persistência: logo após pouso/emergência a última telemetria ainda mostra altura
            if st.h > 40:
                self._air_since = self._air_since or now
                if now - self._air_since > 2.0:
                    self._set_phase(Phase.FLYING)
                    self.event("voo detectado pela telemetria")
            else:
                self._air_since = None

    def _rc_loop(self) -> None:
        period = 1.0 / self.ctrl.rc_hz
        next_t = time.monotonic()
        while self._running:
            now = time.monotonic()
            with self._lock:
                manual, auto = self._manual, self._auto
            stale = manual is None or now - manual.t > self.ctrl.input_stale_s
            m_rc = RC_ZERO if stale or manual is None else manual.rc
            m_active = False if stale or manual is None else manual.active
            rc, source = self.arbiter.decide(m_rc, m_active, auto, now)

            st, _, _ = self.link.state.get()
            d = self.failsafe.evaluate(st, self.link.state_age_s, self.phase == Phase.FLYING, rc,
                                       input_stale=stale and self.phase == Phase.FLYING)
            for a in d.actions:
                if a == "land":
                    self.event("FAILSAFE: bateria crítica -> pouso automático")
                    self._jobs.put("land")
            self.warnings = d.warnings
            self.source = "HOVER" if (stale and self.phase == Phase.FLYING) else source
            self._autodetect_phase(now)

            # durante takeoff/land o Tello executa sozinho; rc só em voo ou no chão (keepalive)
            if self.link.connected and self.phase in (Phase.FLYING, Phase.LANDED):
                try:
                    self.link.rc(d.rc)
                    self.rc_rate.tick(now)
                except OSError as e:
                    log.warning("falha ao enviar rc: %s", e)
            self.last_rc = d.rc

            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()
