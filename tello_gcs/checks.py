"""Checagens de pré-voo compartilhadas entre o app (checklist ao vivo) e o preflight.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import SafetyConfig
from .link.driver import TelloLink


@dataclass
class CheckItem:
    label: str
    status: Optional[bool]   # True ok | False falha (NO-GO) | None aviso/pendente
    detail: str = ""


@dataclass
class DroneInfo:
    model: str
    sdk: Optional[str]
    sn: Optional[str]
    wifi_snr: Optional[str]


def identify(link: TelloLink) -> DroneInfo:
    """Consultas rápidas (~3 RTTs). `sdk?` só existe no SDK 3 (Tello EDU/TT)."""
    sdk = link.send("sdk?", timeout=1.0)
    sn = link.send("sn?", timeout=1.0)
    snr = link.send("wifi?", timeout=1.0)
    if sdk and sdk.strip().isdigit():
        v = int(sdk.strip())
        model = f"EDU/TT (SDK {v // 10}.{v % 10})"
    else:
        model = "Tello (SDK 2.x)"
        sdk = None
    return DroneInfo(model, sdk, sn, snr)


def live_checklist(link: TelloLink, safety: SafetyConfig, detector_ready: Optional[bool],
                   pad_name: Optional[str], recording: bool) -> list[CheckItem]:
    items: list[CheckItem] = []
    rtt = link.last_rtt_ms
    items.append(CheckItem("link", link.connected and link.link_ok, f"RTT {rtt:.0f} ms" if rtt is not None else ""))
    hz = link.state_rate.hz()
    items.append(CheckItem("telemetria", True if hz >= 5 else (None if hz > 0 else False), f"{hz:.1f} Hz"))
    st, _, _ = link.state.get()
    if st is None:
        items.append(CheckItem("bateria", False, "sem dados"))
        items.append(CheckItem("temperatura", None, "sem dados"))
    else:
        items.append(CheckItem("bateria", st.bat >= safety.takeoff_min_battery_pct, f"{st.bat}%"))
        temp_ok = True if st.temph < safety.temp_warn_c else (None if st.temph < safety.temp_critical_c else False)
        items.append(CheckItem("temperatura", temp_ok, f"{st.temph} °C"))
    fps = link.video.fps.hz()
    items.append(CheckItem("vídeo", True if fps >= 15 else (None if fps > 0 else False), f"{fps:.0f} fps"))
    det_detail = {True: "pronto", False: "falhou", None: "carregando/off"}[detector_ready]
    items.append(CheckItem("detector", detector_ready if detector_ready is not None else None, det_detail))
    items.append(CheckItem("controle", True if pad_name else None, pad_name or "só teclado"))
    items.append(CheckItem("gravação", True if recording else None, "on" if recording else "off"))
    return items


def is_go(items: list[CheckItem]) -> bool:
    core = {"link", "telemetria", "bateria", "vídeo"}
    return all(i.status is not False for i in items) and all(i.status is True for i in items if i.label in core)
