"""Configuração central. Valores padrão pensados para voo indoor conservador."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LinkConfig:
    host: str = "192.168.10.1"   # IP do Tello no modo AP
    cmd_port: int = 8889         # comandos (porta do drone)
    state_port: int = 8890       # telemetria (porta local)
    video_port: int = 11111      # vídeo H.264 (porta local)
    cmd_timeout_s: float = 3.0   # timeout padrão de comandos simples
    takeoff_timeout_s: float = 12.0
    land_timeout_s: float = 12.0
    link_lost_s: float = 1.0     # sem telemetria por mais que isso = link perdido


@dataclass
class ControlConfig:
    rc_hz: float = 30.0                          # frequência de envio de `rc`
    deadzone: float = 0.08                       # zona morta dos sticks (0..1)
    expo: float = 0.35                           # curva expo (0 = linear)
    trigger_deadzone: float = 0.05               # zona morta de R2/L2 (subir/descer)
    yaw_button_level: float = 0.8                # yaw com L1/R1 = 80% do modo de velocidade
    speed_modes: tuple[int, ...] = (30, 60, 100)  # % do rc máximo
    default_speed_mode: int = 0                  # começa no modo lento (indoor)
    input_stale_s: float = 0.25                  # entrada sem atualizar -> hover
    override_hold_s: float = 1.0                 # manual pausa a autonomia por N s


@dataclass
class SafetyConfig:
    land_battery_pct: int = 12          # pousa sozinho abaixo disso
    warn_battery_pct: int = 20
    takeoff_min_battery_pct: int = 20   # não decola abaixo disso
    max_height_cm: int = 200            # teto indoor (relativo à decolagem)
    temp_warn_c: int = 75
    temp_critical_c: int = 85


@dataclass
class PerceptionConfig:
    model: str = str(ROOT / "models" / "yolo26n.pt")
    device: str = "auto"   # auto -> mps se disponível
    imgsz: int = 640
    conf: float = 0.35
    enabled: bool = True


@dataclass
class AppConfig:
    link: LinkConfig = field(default_factory=LinkConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    recordings_dir: Path = ROOT / "recordings"
    record: bool = True
