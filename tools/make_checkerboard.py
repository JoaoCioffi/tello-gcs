#!/usr/bin/env python
"""Gera calib/checkerboard.png (10x7 quadrados = 9x6 cantos internos) para exibir em tela cheia ou imprimir.

Depois de exibir, MEÇA o lado de um quadrado com régua e use em --square-mm.
"""
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def main(cols_sq: int = 10, rows_sq: int = 7, px: int = 160, margin: int = 80) -> None:
    w, h = cols_sq * px + 2 * margin, rows_sq * px + 2 * margin
    img = np.full((h, w), 255, np.uint8)
    for r in range(rows_sq):
        for c in range(cols_sq):
            if (r + c) % 2 == 0:
                y, x = margin + r * px, margin + c * px
                img[y:y + px, x:x + px] = 0
    out = ROOT / "calib" / "checkerboard.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    print(f"salvo {out} ({w}x{h}); cantos internos {cols_sq - 1}x{rows_sq - 1}")


if __name__ == "__main__":
    main()
