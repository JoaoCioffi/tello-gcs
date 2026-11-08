"""Calibração intrínseca da câmera (tabuleiro de xadrez), necessária para a Fase 2 (SLAM/profundidade)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np


@dataclass
class Calibration:
    k: np.ndarray            # 3x3
    dist: np.ndarray         # (5,)
    size: tuple[int, int]    # (w, h)
    rms: float
    views: int

    def to_json(self) -> dict:
        return {"K": self.k.tolist(), "dist": self.dist.ravel().tolist(), "size": list(self.size),
                "rms_px": self.rms, "views": self.views,
                "fx": self.k[0, 0], "fy": self.k[1, 1], "cx": self.k[0, 2], "cy": self.k[1, 2]}


def find_corners(rgb: np.ndarray, cols: int, rows: int) -> np.ndarray | None:
    """cols x rows = cantos INTERNOS do tabuleiro."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ok, corners = cv2.findChessboardCornersSB(gray, (cols, rows), flags=cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE)
    return corners.reshape(-1, 2).astype(np.float32) if ok else None


def select_views(images: Iterable[np.ndarray], cols: int, rows: int, max_views: int = 40,
                 min_motion_px: float = 25.0) -> tuple[list[np.ndarray], tuple[int, int]]:
    """Detecta cantos e descarta vistas quase iguais (drone parado gera centenas de frames iguais)."""
    views: list[np.ndarray] = []
    size = (0, 0)
    for img in images:
        size = (img.shape[1], img.shape[0])
        c = find_corners(img, cols, rows)
        if c is None:
            continue
        if views and min(np.linalg.norm(c - v, axis=1).mean() for v in views) < min_motion_px:
            continue
        views.append(c)
        if len(views) >= max_views:
            break
    return views, size


def calibrate(views: list[np.ndarray], size: tuple[int, int], cols: int, rows: int, square_m: float) -> Calibration:
    if len(views) < 8:
        raise ValueError(f"poucas vistas úteis ({len(views)}); precisa de >= 8 com ângulos variados")
    obj = np.zeros((rows * cols, 3), np.float32)
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square_m
    rms, k, dist, _, _ = cv2.calibrateCamera([obj] * len(views), [v.reshape(-1, 1, 2) for v in views], size, None, None)
    return Calibration(k, dist.ravel(), size, float(rms), len(views))
