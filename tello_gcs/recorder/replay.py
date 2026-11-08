"""Servidor de replay: finge ser o Tello reenviando um voo gravado pelo UDP local.

O pipeline inteiro (driver, decoder, detector, HUD) roda sem saber que é replay. Assim dá
para desenvolver percepção/mapeamento/LSTM em cima de voos reais sem gastar bateria.
Comandos recebem "ok" e não têm efeito (o voo já aconteceu).
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from .reader import iter_jsonl, iter_video_datagrams

log = logging.getLogger(__name__)


class ReplayServer:
    def __init__(self, session: Path, host: str = "127.0.0.1", cmd_port: int = 8889, state_port: int = 8890,
                 video_port: int = 11111, speed: float = 1.0, loop: bool = True) -> None:
        self.session = Path(session)
        self.host, self.cmd_port, self.state_port, self.video_port = host, cmd_port, state_port, video_port
        self.speed = speed
        self.loop = loop
        self.client_ip: str | None = None
        self.streaming = False
        self.finished = False
        self._running = False
        self._threads: list[threading.Thread] = []
        timeline: list[tuple[float, int, bytes]] = []  # (t, canal 0=estado 1=vídeo, payload)
        for obj in iter_jsonl(self.session, "state.jsonl"):
            timeline.append((obj["t"], 0, (obj["raw"] + "\r\n").encode()))
        for t, data in iter_video_datagrams(self.session):
            timeline.append((t, 1, data))
        timeline.sort(key=lambda x: x[0])
        if not timeline:
            raise ValueError(f"gravação vazia: {self.session}")
        self.timeline = timeline
        self.duration = timeline[-1][0] - timeline[0][0]
        self._last_state = b""

    def start(self) -> "ReplayServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.cmd_port))
        self._sock.settimeout(0.2)
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = True
        for fn in (self._cmd_loop, self._play_loop):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)
        log.info("replay de %s (%.1f s, %d pacotes)", self.session.name, self.duration, len(self.timeline))
        return self

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=1.0)
        self._sock.close()
        self._out.close()

    def _cmd_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            cmd = data.decode(errors="ignore").strip()
            self.client_ip = addr[0]
            if cmd.startswith("rc "):
                continue
            if cmd == "streamon":
                self.streaming = True
            elif cmd == "streamoff":
                self.streaming = False
            if cmd == "battery?":
                raw = self._last_state.decode(errors="ignore")
                bat = next((kv.split(":")[1] for kv in raw.split(";") if kv.startswith("bat:")), "0")
                self._sock.sendto(bat.encode(), addr)
            elif cmd.endswith("?"):
                self._sock.sendto(b"unknown command: " + cmd.encode(), addr)
            else:
                self._sock.sendto(b"ok", addr)

    def _play_loop(self) -> None:
        while self._running and self.client_ip is None:
            time.sleep(0.05)
        while self._running:
            t0_rec = self.timeline[0][0]
            t0 = time.monotonic()
            for t, ch, payload in self.timeline:
                if not self._running:
                    return
                due = t0 + (t - t0_rec) / self.speed
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if ch == 0:
                    self._last_state = payload
                    self._out.sendto(payload, (self.client_ip, self.state_port))
                elif self.streaming:
                    self._out.sendto(payload, (self.client_ip, self.video_port))
            if not self.loop:
                self.finished = True
                return
