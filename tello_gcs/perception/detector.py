"""Detector de objetos (YOLO via ultralytics) numa thread própria, sempre no frame mais novo."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..bus import Ema, Latest, RateMeter
from ..config import PerceptionConfig
from ..link.video import VideoFrame

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detections:
    frame_index: int
    t_frame: float          # t_recv do frame usado
    boxes: np.ndarray       # (N, 4) xyxy em pixels
    cls: np.ndarray         # (N,) int
    conf: np.ndarray        # (N,) float
    names: dict             # id -> nome
    infer_ms: float

    def labels(self) -> list[str]:
        return [self.names.get(int(c), str(int(c))) for c in self.cls]


def pick_device(pref: str) -> str:
    if pref != "auto":
        return pref
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


class Detector:
    def __init__(self, cfg: PerceptionConfig, frames: Latest[VideoFrame]) -> None:
        self.cfg = cfg
        self.frames = frames
        self.out: Latest[Detections] = Latest()
        self.rate = RateMeter()
        self.infer_ms = Ema(0.1)
        self.enabled = cfg.enabled
        self.device = pick_device(cfg.device)
        self.ready = False
        self._model = None
        self._thread: threading.Thread | None = None
        self._running = False

    def load(self) -> float:
        """Carrega o modelo e faz warmup. Retorna o tempo de warmup em ms."""
        from ultralytics import YOLO

        self._model = YOLO(self.cfg.model)
        dummy = np.zeros((720, 960, 3), np.uint8)
        t0 = time.perf_counter()
        self._model.predict(dummy, device=self.device, imgsz=self.cfg.imgsz, verbose=False)
        self.ready = True
        return (time.perf_counter() - t0) * 1000

    def infer(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict, float]:
        assert self._model is not None
        bgr = np.ascontiguousarray(rgb[..., ::-1])  # ultralytics espera BGR
        t0 = time.perf_counter()
        r = self._model.predict(bgr, device=self.device, imgsz=self.cfg.imgsz, conf=self.cfg.conf, verbose=False)[0]
        ms = (time.perf_counter() - t0) * 1000
        b = r.boxes
        return (b.xyxy.cpu().numpy(), b.cls.cpu().numpy().astype(int), b.conf.cpu().numpy(), r.names, ms)

    def start(self) -> None:
        if self._model is None:
            self.load()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        seq = 0
        while self._running:
            got = self.frames.wait_newer(seq, timeout=0.2)
            if got is None:
                continue
            frame, _, seq = got
            if not self.enabled or frame is None:
                continue
            try:
                boxes, cls, conf, names, ms = self.infer(frame.image)
            except Exception:
                log.exception("inferência falhou")
                time.sleep(0.5)
                continue
            self.infer_ms.update(ms)
            self.rate.tick()
            self.out.publish(Detections(frame.index, frame.t_recv, boxes, cls, conf, names, ms))
