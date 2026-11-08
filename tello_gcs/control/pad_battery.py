"""Bateria do DualSense (PS5) via relatório HID, sem tomar o controle do SDL/pygame.

Como funciona:
- O SDL só lê a bateria quando o controle está em "modo estendido" (relatório BT 0x31).
  A app liga isso com SDL_JOYSTICK_HIDAPI_PS5_RUMBLE=1 (o próprio driver do SDL
  gerencia o modo; os sticks continuam sendo lidos por ele normalmente).
- O SDL 2 só expõe nível grosseiro (empty/low/medium/full/wired). Para ter a %,
  esta thread abre o mesmo dispositivo HID em modo NÃO exclusivo a cada `period_s`,
  lê um relatório, extrai o byte de bateria e fecha.

SEGURANÇA: no macOS o hidapi abre em modo exclusivo (seize) por padrão, o que tiraria
o controle do pygame. Desligamos isso via ctypes e nos recusamos a abrir se não der.

Byte de bateria: BT 0x31 -> data[54] | USB 0x01 (64 B) -> data[53]
  nibble baixo: nível 0..10 (x10% +5) | nibble alto: 0 descarregando, 1 carregando, 2 cheio
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

SONY_VID = 0x054C
DUALSENSE_PIDS = (0x0CE6, 0x0DF2)  # DualSense, DualSense Edge
STATES = {0: "descarregando", 1: "carregando", 2: "carga completa"}


@dataclass(frozen=True)
class PadBattery:
    pct: int
    state: str
    t: float


def parse_battery(report: list[int] | bytes) -> Optional[tuple[int, str]]:
    """Extrai (%, estado) de um relatório de entrada do DualSense, ou None se não tiver bateria."""
    if not report:
        return None
    rid, n = report[0], len(report)
    if rid == 0x31 and n >= 55:
        b = report[54]
    elif rid == 0x01 and n >= 54:
        b = report[53]
    else:
        return None  # relatório BT básico (10 B) não traz bateria
    pct = min((b & 0x0F) * 10 + 5, 100)
    state = STATES.get((b & 0xF0) >> 4, f"erro {(b & 0xF0) >> 4}")
    if state == "carga completa":
        pct = 100
    return pct, state


class PadBatteryMonitor:
    def __init__(self, period_s: float = 5.0) -> None:
        self.period_s = period_s
        self.latest: Optional[PadBattery] = None
        self.available = False
        self.error: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._hid = None

    def start(self) -> "PadBatteryMonitor":
        if not self.setup():
            return self
        self._running = True
        self._thread = threading.Thread(target=self._run, name="pad-battery", daemon=True)
        self._thread.start()
        return self

    def setup(self) -> bool:
        """Prepara o hidapi em modo não exclusivo. Retorna False se indisponível."""
        if self.available:
            return True
        try:
            import hid

            lib = ctypes.CDLL(hid.__file__)
            lib.hid_init()
            if hasattr(lib, "hid_darwin_set_open_exclusive"):
                lib.hid_darwin_set_open_exclusive(ctypes.c_int(0))
                if lib.hid_darwin_get_open_exclusive() != 0:
                    raise RuntimeError("não foi possível desligar o modo exclusivo do hidapi")
            self._hid = hid
            self.available = True
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.warning("bateria do controle indisponível: %s", e)
            return False
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def read_once(self, timeout_s: float = 0.5) -> Optional[PadBattery]:
        hid = self._hid
        if hid is None:
            return None
        info = next((d for d in hid.enumerate(SONY_VID) if d["product_id"] in DUALSENSE_PIDS), None)
        if info is None:
            return None
        dev = hid.device()
        try:
            dev.open_path(info["path"])
            t_end = time.monotonic() + timeout_s
            while time.monotonic() < t_end:
                r = dev.read(128, timeout_ms=50)
                parsed = parse_battery(r) if r else None
                if parsed:
                    return PadBattery(parsed[0], parsed[1], time.monotonic())
            return None
        finally:
            dev.close()

    def _run(self) -> None:
        while self._running:
            try:
                b = self.read_once()
                if b is not None:
                    self.latest = b
            except Exception as e:  # noqa: BLE001
                log.debug("leitura de bateria falhou: %s", e)
            t_next = time.monotonic() + self.period_s
            while self._running and time.monotonic() < t_next:
                time.sleep(0.2)
