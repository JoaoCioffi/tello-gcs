#!/usr/bin/env python
"""Calibra a câmera do Tello a partir de uma gravação de voo (ou pasta de imagens).

Como gravar: abra calib/checkerboard.png em tela cheia (ou imprima), meça o lado de um
quadrado com régua, e durante o voo passe ~15 s apontando para ele de ângulos e distâncias
variadas (inclinado, perto, longe, cantos da imagem).

  python tools/calibrate.py --recording recordings/20261108-153000 --square-mm 30
  python tools/calibrate.py --images recordings/.../snapshots --square-mm 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from tello_gcs.perception.calibration import calibrate, select_views  # noqa: E402
from tello_gcs.recorder.reader import iter_frames, load_meta  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--recording", type=Path)
    src.add_argument("--images", type=Path)
    ap.add_argument("--cols", type=int, default=9, help="cantos internos na horizontal (padrão 9)")
    ap.add_argument("--rows", type=int, default=6, help="cantos internos na vertical (padrão 6)")
    ap.add_argument("--square-mm", type=float, required=True, help="lado do quadrado medido com régua")
    ap.add_argument("--every", type=int, default=5, help="usa 1 a cada N frames")
    ap.add_argument("--max-views", type=int, default=40)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    sn = "tello"
    if args.recording:
        sn = (load_meta(args.recording).get("sn") or "tello").strip()
        frames = (img for i, (_, img) in enumerate(iter_frames(args.recording)) if i % args.every == 0)
    else:
        paths = sorted(p for p in args.images.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        frames = (cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in paths)

    print("procurando o tabuleiro...")
    views, size = select_views(frames, args.cols, args.rows, args.max_views)
    print(f"{len(views)} vistas úteis em {size[0]}x{size[1]}")
    cal = calibrate(views, size, args.cols, args.rows, args.square_mm / 1000.0)
    out = args.out or ROOT / "calib" / f"camera_{sn}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cal.to_json(), indent=2))
    k = cal.k
    print(f"RMS {cal.rms:.3f} px  (bom: < 0.5)\nfx={k[0,0]:.1f} fy={k[1,1]:.1f} cx={k[0,2]:.1f} cy={k[1,2]:.1f}\ndist={cal.dist.round(4).tolist()}\nsalvo em {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
