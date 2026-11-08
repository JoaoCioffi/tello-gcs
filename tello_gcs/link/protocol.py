"""Protocolo Tello SDK (2.0 e 3.0): parsing de telemetria e formatação de comandos.

Telemetria (UDP 8890, ~10 Hz, ASCII):
  Tello comum: pitch:0;roll:0;yaw:0;vgx:0;vgy:0;vgz:0;templ:60;temph:62;tof:10;h:0;bat:87;baro:120.5;time:0;agx:1.0;agy:-3.0;agz:-998.0;
  EDU/TT acrescentam no início: mid:-1;x:-100;y:-100;z:-100;mpry:0,0,0;

Unidades (documentação oficial é vaga; confirmar com voo real na Fase 2):
  pitch/roll/yaw: graus | vgx/vgy/vgz: dm/s | templ/temph: °C | tof/h: cm | bat: %
  baro: m (relativo) | time: s de motor ligado | agx/agy/agz: mg (0.001 g)
"""
from __future__ import annotations

from dataclasses import dataclass, field

TELLO_HOST = "192.168.10.1"
CMD_PORT = 8889
STATE_PORT = 8890
VIDEO_PORT = 11111

TOF_INVALID = 6553  # valor reportado quando o ToF está fora de alcance

_INT_FIELDS = ("pitch", "roll", "yaw", "vgx", "vgy", "vgz", "templ", "temph", "tof", "h", "bat", "time")
_FLOAT_FIELDS = ("baro", "agx", "agy", "agz")


@dataclass(frozen=True)
class TelloState:
    pitch: int = 0
    roll: int = 0
    yaw: int = 0
    vgx: int = 0
    vgy: int = 0
    vgz: int = 0
    templ: int = 0
    temph: int = 0
    tof: int = 0
    h: int = 0
    bat: int = 0
    baro: float = 0.0
    time: int = 0
    agx: float = 0.0
    agy: float = 0.0
    agz: float = 0.0
    extra: dict = field(default_factory=dict)  # mid/x/y/z/mpry (EDU/TT) e campos desconhecidos

    @property
    def tof_valid(self) -> bool:
        return 0 < self.tof < TOF_INVALID


def parse_state(raw: str) -> TelloState:
    vals: dict = {}
    extra: dict = {}
    for item in raw.strip().split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key, value = key.strip(), value.strip()
        try:
            if key in _INT_FIELDS:
                vals[key] = int(float(value))
            elif key in _FLOAT_FIELDS:
                vals[key] = float(value)
            else:
                extra[key] = value
        except ValueError:
            extra[key] = value
    return TelloState(**vals, extra=extra)


def clamp_rc(v: float) -> int:
    return max(-100, min(100, int(round(v))))


@dataclass(frozen=True)
class RcCommand:
    """Comando `rc`: cada eixo em [-100, 100].

    lr: esquerda(-)/direita(+) | fb: trás(-)/frente(+) | ud: desce(-)/sobe(+) | yaw: anti-horário(-)/horário(+)
    """

    lr: int = 0
    fb: int = 0
    ud: int = 0
    yaw: int = 0

    def is_zero(self) -> bool:
        return self.lr == 0 and self.fb == 0 and self.ud == 0 and self.yaw == 0

    def to_cmd(self) -> str:
        return f"rc {clamp_rc(self.lr)} {clamp_rc(self.fb)} {clamp_rc(self.ud)} {clamp_rc(self.yaw)}"

    @staticmethod
    def of(lr: float = 0, fb: float = 0, ud: float = 0, yaw: float = 0) -> "RcCommand":
        return RcCommand(clamp_rc(lr), clamp_rc(fb), clamp_rc(ud), clamp_rc(yaw))


RC_ZERO = RcCommand()
