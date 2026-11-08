#!/usr/bin/env python
"""Tello Ground Station (Fase 1): teleoperação PS5/teclado + HUD + YOLO + gravação.

Exemplos:
  python app.py                      # drone real (Mac conectado no Wi-Fi TELLO-XXXXXX)
  python app.py --sim                # emulador local (sem drone)
  python app.py --replay recordings/20261108-153000
"""
from __future__ import annotations

import os

# precisam estar definidos antes de importar o pygame
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
# DualSense em modo estendido (gerenciado pelo driver do SDL): habilita leitura de bateria
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS5_RUMBLE", "1")  # controle funciona sem foco na janela
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("app")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Tello Ground Station")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--sim", action="store_true", help="usa o emulador local em vez do drone")
    src.add_argument("--replay", type=Path, help="reproduz uma gravação")
    ap.add_argument("--sim-sdk3", action="store_true", help="emulador se passa por Tello EDU/TT")
    ap.add_argument("--sim-video-delay-ms", type=float, default=0.0, help="atraso artificial no vídeo do emulador")
    ap.add_argument("--host", default=None, help="IP do drone (padrão 192.168.10.1)")
    ap.add_argument("--no-detect", action="store_true", help="desliga o YOLO")
    ap.add_argument("--model", default=None, help="pesos YOLO (padrão models/yolo26n.pt)")
    ap.add_argument("--no-record", action="store_true", help="não grava o voo")
    ap.add_argument("--max-height", type=int, default=None, help="teto em cm (padrão 200)")
    ap.add_argument("--speed-mode", type=int, choices=(1, 2, 3), default=None, help="modo de velocidade inicial")
    ap.add_argument("--headless", action="store_true", help="sem janela (testes)")
    ap.add_argument("--duration", type=float, default=None, help="encerra após N segundos")
    ap.add_argument("--script", default=None, help='entrada roteirizada, ex.: "2:takeoff,4:rc 0 40 0 0,6:land"')
    ap.add_argument("--screenshot", type=Path, default=None, help="salva print do HUD (usa --screenshot-at)")
    ap.add_argument("--screenshot-at", type=float, default=5.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if args.headless:
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    import cv2
    import pygame

    from tello_gcs.checks import CheckItem, identify, is_go, live_checklist
    from tello_gcs.config import AppConfig
    from tello_gcs.control.flight import FlightController, Phase
    from tello_gcs.control.inputs import InputManager
    from tello_gcs.control.pad_battery import PadBatteryMonitor
    from tello_gcs.control.script import ScriptedInput
    from tello_gcs.control.types import Action
    from tello_gcs.link.driver import TelloLink
    from tello_gcs.perception.detector import Detector
    from tello_gcs.recorder.record import FlightRecorder
    from tello_gcs.ui.hud import WINDOW, Hud, HudContext

    cfg = AppConfig()
    if args.host:
        cfg.link.host = args.host
    if args.model:
        cfg.perception.model = args.model
    if args.no_detect:
        cfg.perception.enabled = False
    if args.max_height:
        cfg.safety.max_height_cm = args.max_height
    if args.speed_mode:
        cfg.control.default_speed_mode = args.speed_mode - 1
    cfg.record = not args.no_record and not args.replay

    # ------------------------------------------------------------------ fonte de dados
    mode = "AO VIVO"
    server = None
    if args.sim:
        from tello_gcs.sim.emulator import EmuParams, TelloEmulator

        mode = "SIMULADOR"
        cfg.link.host = "127.0.0.1"
        server = TelloEmulator("127.0.0.1", cfg.link.cmd_port, cfg.link.state_port, cfg.link.video_port,
                               sdk3=args.sim_sdk3, params=EmuParams(video_delay_ms=args.sim_video_delay_ms)).start()
    elif args.replay:
        from tello_gcs.recorder.replay import ReplayServer

        mode = "REPLAY"
        cfg.link.host = "127.0.0.1"
        server = ReplayServer(args.replay, "127.0.0.1", cfg.link.cmd_port, cfg.link.state_port, cfg.link.video_port).start()

    recorder = FlightRecorder(cfg.recordings_dir, meta={"mode": mode, "host": cfg.link.host}) if cfg.record else None
    link = TelloLink(
        cfg.link,
        on_command=recorder.on_command if recorder else None,
        on_state_raw=recorder.on_state_raw if recorder else None,
        on_video_datagram=recorder.on_video_datagram if recorder else None,
    )

    pygame.init()
    screen = pygame.display.set_mode(WINDOW)
    pygame.display.set_caption(f"Tello Ground Station — {mode}")
    hud = Hud(screen)
    clock = pygame.time.Clock()
    inputs = InputManager(cfg.control)
    pad_battery = PadBatteryMonitor(period_s=5.0).start()
    t_start = time.monotonic()
    script = ScriptedInput(args.script, t_start) if args.script else None

    def splash(text: str) -> bool:
        """Desenha uma tela de espera; retorna False se o usuário pediu para sair."""
        for ev in pygame.event.get():
            if Action.QUIT in inputs.handle_event(ev):
                return False
        hud.draw(HudContext(mode=mode, host=cfg.link.host, banner=text, phase="-"))
        pygame.display.flip()
        return True

    # ------------------------------------------------------------------ conexão
    while not link.connected:
        if not splash(f"Conectando em {cfg.link.host} ..."):
            link.close()
            if server:
                server.stop()
            pygame.quit()
            return 1
        if not link.connect(attempts=1):
            for _ in range(10):
                if not splash(f"Sem resposta de {cfg.link.host} — confira o Wi-Fi TELLO-xxxx"):
                    link.close()
                    if server:
                        server.stop()
                    pygame.quit()
                    return 1
                time.sleep(0.1)
        if args.duration and time.monotonic() - t_start > args.duration:
            log.error("não conectou")
            link.close()
            pygame.quit()
            return 1

    if recorder:
        recorder.start()
    info = identify(link)
    log.info("drone: %s | sn %s | wifi %s", info.model, info.sn, info.wifi_snr)
    if recorder:
        recorder.update_meta(model=info.model, sdk=info.sdk, sn=info.sn, wifi_snr=info.wifi_snr)
    if not link.stream_on():
        log.warning("streamon sem confirmação")

    # ------------------------------------------------------------------ detector (carrega em paralelo)
    detector = Detector(cfg.perception, link.video.frames) if cfg.perception.enabled else None
    det_ready: list[bool | None] = [None]

    def _load_detector():
        try:
            ms = detector.load()  # type: ignore[union-attr]
            log.info("detector pronto em %s (warmup %.0f ms)", detector.device, ms)  # type: ignore[union-attr]
            detector.start()  # type: ignore[union-attr]
            det_ready[0] = True
        except Exception:
            log.exception("detector falhou")
            det_ready[0] = False

    if detector:
        threading.Thread(target=_load_detector, daemon=True).start()

    flight = FlightController(link, cfg.control, cfg.safety, on_event=recorder.on_event if recorder else None)
    flight.start()
    flight.event(f"conectado: {info.model}")

    # ------------------------------------------------------------------ loop principal
    quitting = False
    quit_deadline = None
    screenshot_done = False
    show_det = True
    exit_code = 0
    try:
        while True:
            now = time.monotonic()
            actions: list[Action] = []
            for ev in pygame.event.get():
                actions += inputs.handle_event(ev)
            if script:
                actions += script.pop_actions(now)

            manual = inputs.poll(now)
            if script and not manual.active:
                manual = script.poll(now)
            flight.set_manual(manual)

            for a in actions:
                if a in (Action.TAKEOFF, Action.LAND, Action.EMERGENCY):
                    if quitting and a == Action.TAKEOFF:
                        continue
                    flight.request(a)
                elif a == Action.TOGGLE_RECORD and recorder:
                    flight.event("gravação " + ("ligada" if recorder.toggle() else "desligada"))
                elif a == Action.TOGGLE_DETECT and detector:
                    detector.enabled = not detector.enabled
                    show_det = detector.enabled
                elif a == Action.SNAPSHOT:
                    fr, _, _ = link.video.frames.get()
                    if fr is not None:
                        d = recorder.snapshot_dir() if recorder else cfg.recordings_dir / "snapshots"
                        d.mkdir(parents=True, exist_ok=True)
                        path = d / f"frame_{fr.index:06d}.png"
                        cv2.imwrite(str(path), cv2.cvtColor(fr.image, cv2.COLOR_RGB2BGR))
                        flight.event(f"foto: {path.name}")
                elif a == Action.QUIT and not quitting:
                    quitting = True
                    if flight.flying and mode != "REPLAY":
                        flight.event("saindo: pousando antes")
                        flight.request(Action.LAND)
                    quit_deadline = now + cfg.link.land_timeout_s + 3

            if args.duration and now - t_start > args.duration and not quitting:
                quitting = True
                if flight.flying and mode != "REPLAY":
                    flight.request(Action.LAND)
                quit_deadline = now + cfg.link.land_timeout_s + 3

            # ---------------------------------------------------------- HUD
            frame, _, fseq = link.video.frames.get()
            dets, _, _ = detector.out.get() if detector else (None, 0, 0)
            st, _, _ = link.state.get()
            items = live_checklist(link, cfg.safety, det_ready[0] if detector else None,
                                   inputs.pads and inputs.device_name or None, bool(recorder and recorder.active))
            items.insert(0, CheckItem("PRONTO P/ VOO", is_go(items), "GO" if is_go(items) else "NO-GO"))
            metrics = {
                "state_hz": link.state_rate.hz(),
                "video_fps": link.video.fps.hz(),
                "decode_err": link.video.decode_errors,
                "rc_hz": flight.rc_rate.hz(),
                "rtt_ms": link.last_rtt_ms,
                "frame_age_ms": (time.monotonic() - frame.t_recv) * 1000 if frame else None,
                "decode_ms": link.video.decode_ms.value,
                "infer_ms": detector.infer_ms.value if detector else None,
                "det_hz": detector.rate.hz() if detector else 0.0,
            }
            # bateria do controle: % via HID; senão nível grosseiro do SDL
            pb_pct, pb_text = None, ""
            if inputs.pads:
                pb = pad_battery.latest
                if pb is not None and now - pb.t < 30:
                    pb_pct, pb_text = pb.pct, pb.state
                else:
                    lvl = inputs.sdl_power_level()
                    pb_text = f"{lvl} (aprox.)" if lvl and lvl != "unknown" else "lendo..."
            warnings = list(flight.warnings)
            if pb_pct is not None and pb_pct <= 20 and pb_text == "descarregando":
                warnings.append(f"BATERIA DO CONTROLE {pb_pct}%")

            banner = ""
            if quitting and flight.flying and mode != "REPLAY":
                banner = "Pousando antes de sair..."
            ctx = HudContext(
                mode=mode, host=cfg.link.host, frame=frame, frame_seq=fseq, detections=dets,
                show_detections=show_det, state=st, state_age_s=link.state_age_s, phase=flight.phase.value,
                source=flight.source, last_rc=flight.last_rc, manual=manual, warnings=warnings,
                events=list(flight.events), checklist=items, recording=bool(recorder and recorder.active),
                model_name=info.model, metrics=metrics, banner=banner,
                pad_battery_pct=pb_pct, pad_battery_text=pb_text,
            )
            hud.draw(ctx)
            pygame.display.flip()

            if args.screenshot and not screenshot_done and now - t_start >= args.screenshot_at:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                pygame.image.save(screen, str(args.screenshot))
                screenshot_done = True
                log.info("screenshot salvo em %s", args.screenshot)

            if quitting:
                landed = mode == "REPLAY" or flight.phase in (Phase.LANDED, Phase.DISCONNECTED)
                if landed or (quit_deadline and now > quit_deadline):
                    if not landed:
                        log.error("timeout pousando antes de sair!")
                        exit_code = 2
                    break
            clock.tick(60)
    except KeyboardInterrupt:
        log.warning("Ctrl+C: pousando")
        if flight.flying:
            flight.request(Action.LAND)
            flight.wait_landed(cfg.link.land_timeout_s)
    finally:
        flight.stop()
        pad_battery.stop()
        if detector:
            detector.stop()
        try:
            if link.connected:
                link.stream_off()
        except Exception:
            pass
        link.close()
        if recorder:
            recorder.stop()
            log.info("voo gravado em %s", recorder.dir)
        if server:
            server.stop()
        pygame.quit()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
