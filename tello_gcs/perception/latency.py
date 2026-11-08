"""Medição de latência "glass-to-glass" do vídeo.

Método: a tela do Mac alterna preto/branco em instantes conhecidos e a câmera do Tello
aponta para ela. Para cada transição detectada no brilho do vídeo, a latência é o tempo
desde a última troca da tela para a mesma cor. Inclui exposição + encoder + Wi-Fi +
decodificação (não inclui o refresh do monitor ao exibir, ~8-16 ms).
"""
from __future__ import annotations

import numpy as np


def frame_brightness(rgb: np.ndarray) -> float:
    h, w = rgb.shape[:2]
    roi = rgb[h // 4: 3 * h // 4: 4, w // 4: 3 * w // 4: 4]
    return float(roi.mean())


def estimate_latency(toggles: list[tuple[float, bool]], samples: list[tuple[float, float]],
                     max_latency_s: float = 0.9) -> dict:
    """toggles: [(t, tela_branca)], samples: [(t_frame, brilho)] -> estatísticas em ms."""
    if len(samples) < 10 or len(toggles) < 2:
        return {"error": "poucas amostras"}
    b = np.array([s[1] for s in samples])
    lo, hi = np.percentile(b, 10), np.percentile(b, 90)
    if hi - lo < 25:
        return {"error": f"contraste insuficiente ({hi - lo:.0f}); aponte a câmera para a tela"}
    hyst = 0.15 * (hi - lo)
    mid = (lo + hi) / 2
    state = b[0] > mid
    transitions: list[tuple[float, bool]] = []
    for t, v in samples:
        if not state and v > mid + hyst:
            state = True
            transitions.append((t, True))
        elif state and v < mid - hyst:
            state = False
            transitions.append((t, False))
    lat = []
    for t, white in transitions:
        prev = [tt for tt, w in toggles if w == white and tt <= t]
        if prev:
            d = t - max(prev)
            if 0 <= d <= max_latency_s:
                lat.append(d * 1000)
    if not lat:
        return {"error": "nenhuma transição casada"}
    a = np.array(lat)
    return {"n": len(a), "median_ms": float(np.median(a)), "p90_ms": float(np.percentile(a, 90)),
            "min_ms": float(a.min()), "max_ms": float(a.max())}
