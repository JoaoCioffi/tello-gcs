"""Recepção e decodificação do vídeo H.264 do Tello com latência mínima.

Decisões de latência:
1. Socket UDP próprio, sem o demuxer do FFmpeg (que faz probing/buffer de entrada).
2. Montagem de frames pelo tamanho do datagrama: o Tello fatia cada frame em datagramas de
   tamanho máximo fixo (1460 B) e o último é menor. Fechar o frame ali evita esperar o
   próximo start code, o que economizaria ~1 frame (33 ms) de atraso.
3. Decoder com flag LOW_DELAY e threading por SLICE (threading por FRAME adiciona N frames de atraso).
4. Publicação em `Latest`: consumidores sempre pegam o frame mais novo.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import av
import av.codec.context
import numpy as np

from ..bus import Ema, Latest, RateMeter

log = logging.getLogger(__name__)

START_CODE = b"\x00\x00\x00\x01"
_MAX_AU_BYTES = 2_000_000


@dataclass(frozen=True)
class VideoFrame:
    image: np.ndarray   # RGB uint8 (H, W, 3)
    index: int
    t_recv: float       # monotonic: chegada do último datagrama do frame
    t_decoded: float    # monotonic: fim da decodificação

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]


class H264Assembler:
    """Agrupa datagramas UDP do Tello em access units (frames) completos."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._max = 0
        self._last_len = 0

    def push(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        # Frame com tamanho múltiplo exato do MTU: o próximo datagrama começa com start code.
        if self._buf and data.startswith(START_CODE) and self._last_len >= self._max:
            out.append(bytes(self._buf))
            self._buf.clear()
        self._max = max(self._max, len(data))
        self._last_len = len(data)
        self._buf += data
        if len(data) < self._max or len(self._buf) > _MAX_AU_BYTES:
            out.append(bytes(self._buf))
            self._buf.clear()
        return out


class H264Decoder:
    def __init__(self) -> None:
        self.codec = av.CodecContext.create("h264", "r")
        self.codec.flags |= av.codec.context.Flags.low_delay
        self.codec.thread_type = "SLICE"
        self.errors = 0

    def decode(self, access_unit: bytes) -> list[np.ndarray]:
        try:
            frames = self.codec.decode(av.Packet(access_unit))
        except (av.error.FFmpegError, ValueError):
            # Perda de pacote: o decoder se recupera no próximo keyframe.
            self.errors += 1
            return []
        return [f.to_ndarray(format="rgb24") for f in frames]


class VideoReceiver:
    def __init__(
        self,
        port: int,
        bind_host: str = "0.0.0.0",
        on_datagram: Optional[Callable[[float, bytes], None]] = None,
    ) -> None:
        self.port = port
        self.bind_host = bind_host
        self.on_datagram = on_datagram
        self.frames: Latest[VideoFrame] = Latest()
        self.fps = RateMeter()
        self.decode_ms = Ema(0.1)
        self.datagrams = 0
        self.bytes = 0
        self.first_datagram_t: float | None = None
        self.first_frame_t: float | None = None
        self._decoder = H264Decoder()
        self._assembler = H264Assembler()
        self._index = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def decode_errors(self) -> int:
        return self._decoder.errors

    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.bind((self.bind_host, self.port))
        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._run, name="video-rx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()
        self._sock = None
        self._thread = None

    def _run(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                data, _ = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            t = time.monotonic()
            if self.first_datagram_t is None:
                self.first_datagram_t = t
            self.datagrams += 1
            self.bytes += len(data)
            if self.on_datagram:
                try:
                    self.on_datagram(t, data)
                except Exception:  # gravação nunca pode derrubar o vídeo
                    log.exception("on_datagram falhou")
            for au in self._assembler.push(data):
                t0 = time.perf_counter()
                images = self._decoder.decode(au)
                if images:
                    self.decode_ms.update((time.perf_counter() - t0) * 1000.0)
                for img in images:
                    now = time.monotonic()
                    self._index += 1
                    if self.first_frame_t is None:
                        self.first_frame_t = now
                    self.fps.tick(now)
                    self.frames.publish(VideoFrame(img, self._index, t, now), now)
