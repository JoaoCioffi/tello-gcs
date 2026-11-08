#!/usr/bin/env python
"""Pré-voo rápido (~5 s com o drone ligado, sem decolar).

Verifica rota de rede, conexão, modelo/SDK, telemetria, bateria, temperatura, vídeo,
detector e controle. Desliga o stream no fim (o Tello parado esquenta).
Com --latency mede a latência glass-to-glass (aponte a câmera para a tela; +8 s).

  python preflight.py              # drone real
  python preflight.py --sim        # contra o emulador
  python preflight.py --latency    # + medição de latência
"""
from __future__ import annotations

import os

os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
# DualSense em modo estendido (gerenciado pelo driver do SDL): habilita leitura de bateria
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS5_RUMBLE", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import argparse
import json
import logging
import socket
import statistics
import sys
import threading
import time
from datetime import datetime

OKS, BADS, WARNS = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


def line(status, label, detail=""):
    sym = OKS if status is True else (BADS if status is False else WARNS)
    print(f"  {sym} {label:<22} {detail}")


def local_ip_towards(host: str) -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 8889))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pré-voo rápido do Tello")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--host", default=None)
    ap.add_argument("--latency", action="store_true", help="mede latência glass-to-glass (precisa de tela)")
    ap.add_argument("--no-detect", action="store_true")
    ap.add_argument("--seconds", type=float, default=3.0, help="janela de medição de vídeo/telemetria")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from tello_gcs.checks import identify
    from tello_gcs.config import AppConfig
    from tello_gcs.link.driver import TelloLink

    cfg = AppConfig()
    if args.host:
        cfg.link.host = args.host
    emu = None
    if args.sim:
        from tello_gcs.sim.emulator import TelloEmulator

        cfg.link.host = "127.0.0.1"
        emu = TelloEmulator("127.0.0.1", cfg.link.cmd_port, cfg.link.state_port, cfg.link.video_port).start()

    report: dict = {"time": datetime.now().isoformat(), "host": cfg.link.host}
    nogo: list[str] = []
    t_begin = time.monotonic()
    print(f"\nPRÉ-VOO — {cfg.link.host}\n")

    # detector carrega em paralelo (warmup do MPS leva ~2 s)
    detector = None
    det_info: dict = {}
    if not args.no_detect:
        from tello_gcs.bus import Latest
        from tello_gcs.perception.detector import Detector

        detector = Detector(cfg.perception, Latest())

        def _load():
            try:
                det_info["warmup_ms"] = detector.load()
            except Exception as e:  # noqa: BLE001
                det_info["error"] = str(e)

        det_thread = threading.Thread(target=_load, daemon=True)
        det_thread.start()

    # 1. rede
    ip = local_ip_towards(cfg.link.host)
    real = not args.sim
    route_ok = ip is not None and (not real or ip.startswith("192.168.10."))
    line(route_ok if real else True, "rota de rede", f"IP local {ip}" + ("" if route_ok or not real else "  (conecte o Mac no Wi-Fi TELLO-xxxx)"))

    link = TelloLink(cfg.link)
    try:
        # 2. conexão
        if not link.connect(attempts=3):
            line(False, "conexão SDK", "sem resposta ao 'command'")
            print("\nNO-GO: drone não respondeu. Está ligado? Mac no Wi-Fi do Tello?\n")
            return 2
        rtts = []
        for _ in range(5):
            if link.send("battery?", timeout=1.0) is not None and link.last_rtt_ms is not None:
                rtts.append(link.last_rtt_ms)
        rtt = statistics.median(rtts) if rtts else None
        report["rtt_ms"] = rtt
        line(True, "conexão SDK", f"RTT mediano {rtt:.1f} ms" if rtt is not None else "")

        # 3. identificação
        info = identify(link)
        report.update(model=info.model, sdk=info.sdk, sn=info.sn, wifi_snr=info.wifi_snr)
        line(True, "modelo", f"{info.model}  sn={info.sn}")
        snr_ok = None
        try:
            snr_ok = int(info.wifi_snr or 0) >= 30
        except ValueError:
            pass
        line(snr_ok if snr_ok is not None else None, "sinal Wi-Fi (SNR)", str(info.wifi_snr))

        # 4+5. telemetria e vídeo medidos juntos
        t_stream = time.monotonic()
        if not link.stream_on():
            line(False, "streamon", "sem 'ok'")
        time.sleep(args.seconds)
        st, _, _ = link.state.get()
        hz = link.state_rate.hz()
        line(hz >= 5, "telemetria", f"{hz:.1f} Hz")
        if hz < 5:
            nogo.append("telemetria")
        if st is not None:
            bat_ok = st.bat >= cfg.safety.takeoff_min_battery_pct
            line(bat_ok, "bateria", f"{st.bat}%  (mín. p/ decolar {cfg.safety.takeoff_min_battery_pct}%)")
            if not bat_ok:
                nogo.append("bateria")
            t_ok = True if st.temph < cfg.safety.temp_warn_c else (None if st.temph < cfg.safety.temp_critical_c else False)
            line(t_ok, "temperatura", f"{st.templ}-{st.temph} °C")
            if t_ok is False:
                nogo.append("temperatura")
            line(True, "altura / ToF", f"h={st.h} cm  tof={st.tof} cm  (no chão: ~0 / ~10)")
            line(True, "atitude", f"pitch {st.pitch}  roll {st.roll}  yaw {st.yaw}")
            report["state"] = {k: getattr(st, k) for k in ("bat", "templ", "temph", "tof", "h", "pitch", "roll", "yaw")}
        v = link.video
        fps = v.fps.hz()
        frame, _, _ = v.frames.get()
        ttff = (v.first_frame_t - t_stream) * 1000 if v.first_frame_t else None
        vid_ok = fps >= 20
        line(vid_ok if fps > 0 else False, "vídeo",
             f"{fps:.1f} fps  {frame.size[0]}x{frame.size[1]}  1º frame {ttff:.0f} ms  erros {v.decode_errors}"
             if frame else "nenhum frame decodificado")
        if fps < 15:
            nogo.append("vídeo")
        if v.decode_ms.value is not None:
            line(True, "decodificação", f"{v.decode_ms.value:.1f} ms/frame")
        report["video"] = {"fps": fps, "first_frame_ms": ttff, "decode_errors": v.decode_errors,
                           "decode_ms": v.decode_ms.value, "size": frame.size if frame else None}

        # 6. detector
        if detector is not None:
            det_thread.join(timeout=15)
            if "error" in det_info or not detector.ready:
                line(False, "detector", det_info.get("error", "não carregou"))
            elif frame is not None:
                times, labels = [], []
                for _ in range(5):
                    fr, _, _ = v.frames.get()
                    _, cls, conf, names, ms = detector.infer(fr.image)
                    times.append(ms)
                    labels = [names[int(c)] for c in cls]
                line(True, "detector", f"{statistics.median(times):.1f} ms em {detector.device}  vê: {', '.join(labels[:5]) or 'nada'}")
                report["detector_ms"] = statistics.median(times)

        # 7. latência glass-to-glass (opcional)
        if args.latency:
            if args.sim:
                line(None, "latência", "não disponível no simulador")
            else:
                report["latency"] = measure_latency(link)
                lat = report["latency"]
                if "error" in lat:
                    line(None, "latência", lat["error"])
                else:
                    line(True, "latência glass-to-glass",
                         f"mediana {lat['median_ms']:.0f} ms  p90 {lat['p90_ms']:.0f} ms  (n={lat['n']})")

        # 8. controle
        import pygame
        from pygame._sdl2 import controller

        pygame.init()
        controller.init()
        pads = []
        for i in range(pygame.joystick.get_count()):
            if controller.is_controller(i):
                c = controller.Controller(i)
                c.rumble(0.0, 0.0, 1)  # ativa modo estendido (bateria)
                pads.append(c.name)
        bat_txt = ""
        if pads:
            from tello_gcs.control.pad_battery import PadBatteryMonitor

            t_end = time.monotonic() + 1.0
            while time.monotonic() < t_end:
                pygame.event.pump()
                time.sleep(0.05)
            mon = PadBatteryMonitor()
            b = mon.read_once() if mon.setup() else None
            if b:
                bat_txt = f"  bateria {b.pct}% ({b.state})"
                report["gamepad_battery"] = b.pct
        line(True if pads else None, "controle", (", ".join(pads) + bat_txt) if pads else "nenhum (só teclado)")
        report["gamepads"] = pads
    finally:
        try:
            if link.connected:
                link.stream_off()
        except Exception:
            pass
        link.close()
        if emu:
            emu.stop()

    elapsed = time.monotonic() - t_begin
    go = not nogo
    report["go"] = go
    report["nogo"] = nogo
    out = cfg.recordings_dir / "preflight"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    verdict = "\033[32mGO\033[0m" if go else f"\033[31mNO-GO\033[0m ({', '.join(nogo)})"
    print(f"\n  {verdict}   ({elapsed:.1f} s, stream desligado)   relatório: {path.relative_to(cfg.recordings_dir.parent)}\n")
    return 0 if go else 1


def measure_latency(link, toggles_n: int = 8, period_s: float = 1.0) -> dict:
    """Pisca a tela preto/branco e mede quando o vídeo do drone vê a troca."""
    import pygame

    from tello_gcs.perception.latency import estimate_latency, frame_brightness

    print("    -> aponte a câmera do drone para a janela que vai piscar (8 s)...")
    pygame.init()
    screen = pygame.display.set_mode((900, 700))
    pygame.display.set_caption("Medição de latência — aponte a câmera para cá")
    samples: list[tuple[float, float]] = []
    toggles: list[tuple[float, bool]] = []
    stop = threading.Event()

    def sampler():
        seq = 0
        while not stop.is_set():
            got = link.video.frames.wait_newer(seq, timeout=0.2)
            if got is None:
                continue
            fr, _, seq = got
            samples.append((fr.t_decoded, frame_brightness(fr.image)))

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    white = False
    screen.fill((0, 0, 0))
    pygame.display.flip()
    time.sleep(1.5)  # deixa a exposição estabilizar
    for _ in range(toggles_n):
        white = not white
        screen.fill((255, 255, 255) if white else (0, 0, 0))
        pygame.display.flip()
        toggles.append((time.monotonic(), white))
        t_end = time.monotonic() + period_s
        while time.monotonic() < t_end:
            pygame.event.pump()
            time.sleep(0.005)
    stop.set()
    th.join(timeout=1)
    pygame.display.quit()
    return estimate_latency(toggles, samples, max_latency_s=period_s * 0.9)


if __name__ == "__main__":
    sys.exit(main())
