"""Emulador do Tello sobre UDP real (mesmo protocolo do drone).

Permite testar o pipeline inteiro (driver, decoder, controle, HUD, gravação) sem ligar o
drone — que superaquece parado e tem bateria curta. Para usar o drone real basta trocar o
host de 127.0.0.1 para 192.168.10.1.

Simula: comandos SDK (command/takeoff/land/emergency/rc/streamon/consultas), timeout de 15 s
sem comandos, dinâmica de 1ª ordem, bateria, temperatura (sobe parado, cai voando) e vídeo
H.264 (libx264 zerolatency) fatiado em datagramas de 1460 B como o Tello faz.
"""
from __future__ import annotations

import heapq
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass

import av
import numpy as np

from ..link.protocol import parse_state  # noqa: F401  (documenta o formato)
from .scene import SIM_SIZE, Scene, rot_wb

log = logging.getLogger(__name__)

VIDEO_CHUNK = 1460


@dataclass
class EmuParams:
    max_h_speed: float = 1.5        # m/s com rc=100
    max_v_speed: float = 1.0        # m/s
    max_yaw_rate: float = 100.0     # graus/s
    tau: float = 0.25               # constante de tempo da resposta de velocidade (s)
    takeoff_height: float = 0.8     # m
    takeoff_time: float = 2.5       # s (real: ~4-6 s)
    battery: float = 85.0           # %
    temp: float = 50.0              # °C
    cmd_timeout_s: float = 15.0
    video_delay_ms: float = 0.0     # atraso extra artificial no vídeo
    fps: float = 30.0


class TelloEmulator:
    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 8889, state_port: int = 8890,
                 video_port: int = 11111, sdk3: bool = False, params: EmuParams | None = None,
                 render: bool = True) -> None:
        self.host, self.cmd_port, self.state_port, self.video_port = host, cmd_port, state_port, video_port
        self.sdk3 = sdk3
        self.p = params or EmuParams()
        self.render_enabled = render
        self.scene = Scene() if render else None
        self._lock = threading.Lock()
        # estado físico
        self.pos = np.zeros(3)
        self.yaw = 0.0                      # rad, anti-horário +
        self.v_body = np.zeros(3)           # m/s (FLU)
        self.rc = (0, 0, 0, 0)
        self.mode = "landed"                # landed | takeoff | flying | landing
        self.motors = False
        self.motor_time = 0.0
        self.battery = self.p.battery
        self.temp = self.p.temp
        self.accel_body = np.zeros(3)
        self.sdk_mode = False
        self.streaming = False
        self.client_ip: str | None = None
        self.last_cmd_t = time.monotonic()
        self.commands: list[str] = []      # histórico (para testes)
        self._running = False
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        self._out: socket.socket | None = None

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> "TelloEmulator":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.cmd_port))
        self._sock.settimeout(0.2)
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._out.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        self._running = True
        for fn, name in ((self._cmd_loop, "emu-cmd"), (self._physics_loop, "emu-phys"), (self._video_loop, "emu-video")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        log.info("emulador Tello em %s:%d", self.host, self.cmd_port)
        return self

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=1.0)
        for s in (self._sock, self._out):
            if s:
                s.close()

    # ------------------------------------------------------------------ comandos
    def _reply(self, addr, text: str) -> None:
        if self._sock and self._running:
            try:
                self._sock.sendto(text.encode(), addr)
            except OSError:
                pass

    def _cmd_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(1024)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            cmd = data.decode("ascii", errors="ignore").strip()
            with self._lock:
                self.last_cmd_t = time.monotonic()
                self.client_ip = addr[0]
                self.commands.append(cmd)
            self._handle(cmd, addr)

    def _handle(self, cmd: str, addr) -> None:
        parts = cmd.split()
        if not parts:
            return
        name = parts[0]
        if name == "command":
            self.sdk_mode = True
            return self._reply(addr, "ok")
        if not self.sdk_mode:
            return
        if name == "rc" and len(parts) == 5:
            try:
                vals = tuple(max(-100, min(100, int(v))) for v in parts[1:])
            except ValueError:
                return
            with self._lock:
                self.rc = vals  # type: ignore[assignment]
            return  # rc não tem resposta
        if name == "streamon":
            self.streaming = True
            return self._reply(addr, "ok")
        if name == "streamoff":
            self.streaming = False
            return self._reply(addr, "ok")
        if name == "takeoff":
            threading.Thread(target=self._do_takeoff, args=(addr,), daemon=True).start()
            return
        if name == "land":
            threading.Thread(target=self._do_land, args=(addr,), daemon=True).start()
            return
        if name == "emergency":
            with self._lock:
                self.mode, self.motors, self.rc = "landed", False, (0, 0, 0, 0)
                self.pos[2] = 0.0
                self.v_body[:] = 0
            return self._reply(addr, "ok")
        if name == "battery?":
            return self._reply(addr, str(int(self.battery)))
        if name == "wifi?":
            return self._reply(addr, "90")
        if name == "sn?":
            return self._reply(addr, "0TQEMULATOR0001")
        if name == "sdk?":
            return self._reply(addr, "30" if self.sdk3 else "unknown command: sdk?")
        if name in ("motoron", "motoroff") and self.sdk3:
            with self._lock:
                if self.mode == "landed":
                    self.motors = name == "motoron"
            return self._reply(addr, "ok")
        return self._reply(addr, f"unknown command: {cmd}")

    def _do_takeoff(self, addr) -> None:
        with self._lock:
            if self.mode != "landed":
                return self._reply(addr, "error")
            if self.battery < 10:
                return self._reply(addr, "error battery low")
            self.mode, self.motors = "takeoff", True
        t_end = time.monotonic() + self.p.takeoff_time + 2.0
        while self._running and time.monotonic() < t_end:
            with self._lock:
                if self.mode != "takeoff":
                    return self._reply(addr, "error")
                if self.pos[2] >= self.p.takeoff_height - 0.02:
                    self.mode = "flying"
                    break
            time.sleep(0.02)
        self._reply(addr, "ok")

    def _do_land(self, addr) -> None:
        with self._lock:
            if self.mode not in ("flying", "takeoff"):
                return self._reply(addr, "error Not flying" if self.mode == "landed" else "error")
            self.mode = "landing"
        while self._running:
            with self._lock:
                if self.mode == "landed":
                    break
                if self.mode != "landing":
                    return
            time.sleep(0.02)
        self._reply(addr, "ok")

    # ------------------------------------------------------------------ física + telemetria
    def _physics_loop(self) -> None:
        dt = 0.02
        next_state = time.monotonic()
        t_prev = time.monotonic()
        while self._running:
            time.sleep(dt)
            now = time.monotonic()
            step = min(now - t_prev, 0.1)
            t_prev = now
            with self._lock:
                self._step(step, now)
                if self.sdk_mode and self.client_ip and now >= next_state:
                    next_state = now + 0.1  # ~10 Hz como o Tello real
                    msg = self._state_string()
                else:
                    msg = None
            if msg and self._out:
                try:
                    self._out.sendto(msg.encode(), (self.client_ip, self.state_port))
                except OSError:
                    pass

    def _step(self, dt: float, now: float) -> None:
        p = self.p
        lr, fb, ud, yw = self.rc
        target = np.zeros(3)
        yaw_rate = 0.0
        if self.mode == "flying":
            target = np.array([fb / 100 * p.max_h_speed, -lr / 100 * p.max_h_speed, ud / 100 * p.max_v_speed])
            yaw_rate = -yw / 100 * math.radians(p.max_yaw_rate)
            if now - self.last_cmd_t > p.cmd_timeout_s:
                log.info("emulador: 15 s sem comandos -> pouso automático")
                self.mode = "landing"
            if self.battery <= 8:
                self.mode = "landing"
        elif self.mode == "takeoff":
            target[2] = p.takeoff_height / p.takeoff_time * 1.3
        elif self.mode == "landing":
            target[2] = -0.5
        prev = self.v_body.copy()
        self.v_body += (target - self.v_body) * min(1.0, dt / p.tau)
        self.accel_body = (self.v_body - prev) / max(dt, 1e-3)
        self.yaw = (self.yaw + yaw_rate * dt + math.pi) % (2 * math.pi) - math.pi
        if self.mode != "landed":
            c, s = math.cos(self.yaw), math.sin(self.yaw)
            vw = np.array([c * self.v_body[0] - s * self.v_body[1], s * self.v_body[0] + c * self.v_body[1], self.v_body[2]])
            self.pos += vw * dt
            r = self.scene.room if self.scene else None
            if r:
                self.pos[0] = np.clip(self.pos[0], r.x0 + 0.3, r.x1 - 0.3)
                self.pos[1] = np.clip(self.pos[1], r.y0 + 0.3, r.y1 - 0.3)
            self.pos[2] = min(self.pos[2], 2.6)
            if self.pos[2] <= 0.0:
                self.pos[2] = 0.0
                if self.mode == "landing":
                    self.mode, self.motors = "landed", False
                    self.v_body[:] = 0
        # bateria e temperatura
        if self.motors:
            self.motor_time += dt
            self.battery -= dt * 100 / (10 * 60)    # ~10 min de voo
            temp_target = 58.0                       # hélices resfriam
        else:
            self.battery -= dt * 0.01
            temp_target = 92.0                       # parado esquenta
        self.battery = max(self.battery, 0.0)
        self.temp += (temp_target - self.temp) * dt / 240.0

    def _attitude_deg(self) -> tuple[float, float]:
        pitch = 8.0 * self.v_body[0] + 1.5 * self.accel_body[0]   # nariz p/ baixo ao acelerar p/ frente
        roll = -8.0 * self.v_body[1] - 1.5 * self.accel_body[1]   # direita p/ baixo ao ir p/ direita
        return float(np.clip(pitch, -25, 25)), float(np.clip(roll, -25, 25))

    def _state_string(self) -> str:
        pitch, roll = self._attitude_deg()
        z_cm = int(self.pos[2] * 100)
        yaw_deg = int(round(-math.degrees(self.yaw)))  # Tello: horário +
        s = (
            f"pitch:{int(round(pitch))};roll:{int(round(roll))};yaw:{yaw_deg};"
            f"vgx:{int(round(self.v_body[0] * 10))};vgy:{int(round(-self.v_body[1] * 10))};vgz:{int(round(-self.v_body[2] * 10))};"
            f"templ:{int(self.temp) - 2};temph:{int(self.temp)};tof:{max(10, z_cm)};h:{z_cm};bat:{int(self.battery)};"
            f"baro:{100.0 + self.pos[2]:.2f};time:{int(self.motor_time)};"
            f"agx:{self.accel_body[0] * 102:.2f};agy:{self.accel_body[1] * 102:.2f};agz:{-1000.0 + self.accel_body[2] * 102:.2f};\r\n"
        )
        if self.sdk3:
            s = "mid:-1;x:-100;y:-100;z:-100;mpry:0,0,0;" + s
        return s

    def snapshot(self) -> dict:
        with self._lock:
            return {"pos": self.pos.copy(), "yaw": self.yaw, "mode": self.mode, "battery": self.battery,
                    "temp": self.temp, "rc": self.rc, "streaming": self.streaming}

    # ------------------------------------------------------------------ vídeo
    def _make_encoder(self):
        enc = av.CodecContext.create("libx264", "w")
        enc.width, enc.height = SIM_SIZE
        enc.pix_fmt = "yuv420p"
        from fractions import Fraction
        enc.time_base = Fraction(1, int(self.p.fps))
        enc.framerate = Fraction(int(self.p.fps), 1)
        enc.options = {"preset": "ultrafast", "tune": "zerolatency",
                       "x264-params": "keyint=30:min-keyint=30:repeat-headers=1:bframes=0"}
        return enc

    def _video_loop(self) -> None:
        enc = None
        period = 1.0 / self.p.fps
        next_t = time.monotonic()
        pts = 0
        pending: list[tuple[float, int, bytes]] = []  # heap (enviar_em, seq, chunk)
        seq = 0
        while self._running:
            now = time.monotonic()
            # envia chunks atrasados que já venceram
            while pending and pending[0][0] <= now:
                _, _, chunk = heapq.heappop(pending)
                self._send_video(chunk)
            if not self.streaming or not self.client_ip:
                enc = None
                time.sleep(0.02)
                next_t = time.monotonic()
                continue
            if now < next_t:
                time.sleep(min(next_t - now, 0.005))
                continue
            next_t += period
            if now - next_t > 0.5:
                next_t = now
            if enc is None:
                enc = self._make_encoder()
                pts = 0
            img = self._render_frame()
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = pts
            pts += 1
            for pkt in enc.encode(frame):
                data = bytes(pkt)
                for i in range(0, len(data), VIDEO_CHUNK):
                    chunk = data[i:i + VIDEO_CHUNK]
                    if self.p.video_delay_ms > 0:
                        seq += 1
                        heapq.heappush(pending, (now + self.p.video_delay_ms / 1000.0, seq, chunk))
                    else:
                        self._send_video(chunk)

    def _send_video(self, chunk: bytes) -> None:
        if self._out and self.client_ip:
            try:
                self._out.sendto(chunk, (self.client_ip, self.video_port))
            except OSError:
                pass

    def _render_frame(self) -> np.ndarray:
        with self._lock:
            pos = self.pos.copy()
            yaw = self.yaw
            pitch, roll = self._attitude_deg()
        if not self.scene:
            img = np.full((SIM_SIZE[1], SIM_SIZE[0], 3), 90, np.uint8)
            return img
        cam = pos + np.array([0.0, 0.0, 0.05])  # câmera um pouco acima do centro
        if cam[2] < 0.05:
            cam[2] = 0.05
        r = rot_wb(yaw, math.radians(pitch) * 0.5, math.radians(roll) * 0.5)
        return self.scene.render(cam, r)


def main() -> None:  # python -m tello_gcs.sim.emulator
    import argparse

    ap = argparse.ArgumentParser(description="Emulador do Tello (UDP)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sdk3", action="store_true", help="emula Tello EDU/TT")
    ap.add_argument("--video-delay-ms", type=float, default=0.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    emu = TelloEmulator(args.host, sdk3=args.sdk3, params=EmuParams(video_delay_ms=args.video_delay_ms)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        emu.stop()


if __name__ == "__main__":
    main()
