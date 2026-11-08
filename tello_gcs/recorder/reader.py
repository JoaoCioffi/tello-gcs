"""Leitura offline de gravações (base para mapeamento, calibração e LSTM nas próximas fases)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import numpy as np

from ..link.protocol import TelloState, parse_state
from ..link.video import H264Assembler, H264Decoder
from .record import VIDEO_HDR


def iter_video_datagrams(session: Path) -> Iterator[tuple[float, bytes]]:
    with open(Path(session) / "video.bin", "rb") as f:
        while True:
            hdr = f.read(VIDEO_HDR.size)
            if len(hdr) < VIDEO_HDR.size:
                return
            t, n = VIDEO_HDR.unpack(hdr)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, data


def iter_frames(session: Path) -> Iterator[tuple[float, np.ndarray]]:
    """(t_recv, imagem RGB) para cada frame decodificado, na ordem de chegada."""
    asm, dec = H264Assembler(), H264Decoder()
    for t, data in iter_video_datagrams(session):
        for au in asm.push(data):
            for img in dec.decode(au):
                yield t, img


def iter_states(session: Path) -> Iterator[tuple[float, TelloState]]:
    with open(Path(session) / "state.jsonl") as f:
        for line in f:
            obj = json.loads(line)
            yield obj["t"], parse_state(obj["raw"])


def iter_jsonl(session: Path, name: str) -> Iterator[dict]:
    p = Path(session) / name
    if not p.exists():
        return
    with open(p) as f:
        for line in f:
            yield json.loads(line)


def load_meta(session: Path) -> dict:
    return json.loads((Path(session) / "meta.json").read_text())
