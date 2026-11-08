"""Driver UDP do Tello (substitui o djitellopy para ter controle total de timing).

- Comandos: socket próprio -> host:8889. Comandos bloqueantes esperam a resposta;
  `rc` e `emergency` são fire-and-forget (não bloqueiam o loop de controle).
- Telemetria: thread dedicada em :8890, cada pacote com timestamp monotônico.
- Vídeo: `VideoReceiver` em :11111.
Tudo é publicado em `Latest` e pode ser gravado via callbacks (recorder).
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

from ..bus import Latest, RateMeter
from ..config import LinkConfig
from .protocol import RcCommand, TelloState, parse_state
from .video import VideoReceiver

log = logging.getLogger(__name__)

# comandos cuja resposta só chega quando a manobra termina (não servem para medir RTT)
_LONG_COMMANDS = {"takeoff", "land", "emergency", "motoron", "motoroff"}


class TelloLink:
    def __init__(
        self,
        cfg: LinkConfig,
        on_command: Optional[Callable[[float, str], None]] = None,
        on_state_raw: Optional[Callable[[float, str], None]] = None,
        on_video_datagram: Optional[Callable[[float, bytes], None]] = None,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.cfg = cfg
        self.on_command = on_command
        self.on_state_raw = on_state_raw
        self.bind_host = bind_host
        self.state: Latest[TelloState] = Latest()
        self.state_rate = RateMeter()
        self.video = VideoReceiver(cfg.video_port, bind_host, on_datagram=on_video_datagram)
        self.connected = False
        self.streaming = False
        self.last_rtt_ms: float | None = None
        self.last_state_t: float = 0.0
        self._responses: "queue.Queue[tuple[float, str]]" = queue.Queue()
        self._cmd_lock = threading.Lock()
        self._cmd_sock: socket.socket | None = None
        self._state_sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------ ciclo de vida
    def open(self) -> None:
        if self._running:
            return
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind((self.bind_host, 0))  # porta efêmera; o Tello responde à origem
        self._cmd_sock.settimeout(0.2)
        self._state_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._state_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._state_sock.bind((self.bind_host, self.cfg.state_port))
        self._state_sock.settimeout(0.2)
        self._running = True
        for target, name in ((self._rx_responses, "cmd-rx"), (self._rx_state, "state-rx")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def connect(self, attempts: int = 3) -> bool:
        """Entra no modo SDK. Retorna True se o drone respondeu 'ok'."""
        self.open()
        for i in range(attempts):
            resp = self.send("command", timeout=1.5)
            if resp == "ok":
                self.connected = True
                log.info("conectado em %s (RTT %.1f ms)", self.cfg.host, self.last_rtt_ms or -1)
                return True
            log.warning("tentativa %d de conexão sem resposta (%r)", i + 1, resp)
        return False

    def close(self) -> None:
        self._running = False
        self.video.stop()
        for th in self._threads:
            th.join(timeout=1.0)
        for s in (self._cmd_sock, self._state_sock):
            if s:
                s.close()
        self._threads.clear()
        self.connected = False
        self.streaming = False

    # ------------------------------------------------------------------ comandos
    def _sendto(self, cmd: str) -> None:
        assert self._cmd_sock is not None, "chame open()/connect() antes"
        t = time.monotonic()
        self._cmd_sock.sendto(cmd.encode("ascii"), (self.cfg.host, self.cfg.cmd_port))
        if self.on_command:
            try:
                self.on_command(t, cmd)
            except Exception:
                log.exception("on_command falhou")

    def send(self, cmd: str, timeout: float | None = None) -> Optional[str]:
        """Envia e espera resposta. Não use com `rc` (não tem resposta)."""
        timeout = self.cfg.cmd_timeout_s if timeout is None else timeout
        with self._cmd_lock:
            while True:  # descarta respostas atrasadas de comandos anteriores
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    break
            t0 = time.monotonic()
            self._sendto(cmd)
            try:
                t, resp = self._responses.get(timeout=timeout)
            except queue.Empty:
                log.warning("sem resposta para %r em %.1fs", cmd, timeout)
                return None
            if cmd.split()[0] not in _LONG_COMMANDS:
                self.last_rtt_ms = (t - t0) * 1000.0
            return resp

    def send_nowait(self, cmd: str) -> None:
        self._sendto(cmd)

    def rc(self, cmd: RcCommand) -> None:
        self._sendto(cmd.to_cmd())

    def emergency(self) -> None:
        """Corta os motores imediatamente. Enviado 3x (UDP pode perder pacote)."""
        for _ in range(3):
            self._sendto("emergency")

    def stream_on(self) -> bool:
        self.video.start()  # escuta antes de pedir o stream para não perder o 1º keyframe
        ok = self.send("streamon") == "ok"
        self.streaming = ok
        return ok

    def stream_off(self) -> bool:
        ok = self.send("streamoff") == "ok"
        self.streaming = False
        return ok

    # ------------------------------------------------------------------ telemetria
    @property
    def state_age_s(self) -> float:
        return time.monotonic() - self.last_state_t if self.last_state_t else float("inf")

    @property
    def link_ok(self) -> bool:
        return self.state_age_s < self.cfg.link_lost_s

    # ------------------------------------------------------------------ threads
    def _rx_responses(self) -> None:
        while self._running:
            try:
                data, _ = self._cmd_sock.recvfrom(1024)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="ignore").strip()
            self._responses.put((time.monotonic(), text))

    def _rx_state(self) -> None:
        while self._running:
            try:
                data, _ = self._state_sock.recvfrom(2048)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            t = time.monotonic()
            raw = data.decode("ascii", errors="ignore")
            try:
                st = parse_state(raw)
            except Exception:
                log.debug("estado inválido: %r", raw)
                continue
            self.last_state_t = t
            self.state_rate.tick(t)
            self.state.publish(st, t)
            if self.on_state_raw:
                try:
                    self.on_state_raw(t, raw)
                except Exception:
                    log.exception("on_state_raw falhou")
