"""Gravador de voo: tudo que entra e sai do drone, com timestamp monotônico.

Formato de uma sessão (recordings/AAAAMMDD-HHMMSS/):
  meta.json      info da sessão (início em wall-clock + monotônico, host, modelo, notas)
  video.bin      datagramas H.264 crus: repetição de [t: float64][len: uint32][bytes]
  state.jsonl    {"t": ..., "raw": "pitch:0;roll:0;..."}
  cmds.jsonl     {"t": ..., "cmd": "rc 0 30 0 0"}
  events.jsonl   {"t": ..., "text": "decolando"}
  snapshots/     fotos (PNG)

Gravar os datagramas crus (em vez de frames decodificados) custa ~zero de CPU e permite
reprodução bit-a-bit do voo pelo mesmo pipeline (ver replay.py).
"""
from __future__ import annotations

import json
import logging
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

VIDEO_HDR = struct.Struct("<dI")


class FlightRecorder:
    def __init__(self, root: Path, meta: dict | None = None) -> None:
        self.root = Path(root)
        self.meta = meta or {}
        self.dir: Path | None = None
        self.active = False
        self._lock = threading.Lock()
        self._files: dict = {}
        self.video_bytes = 0

    def start(self) -> Path:
        with self._lock:
            if self.active:
                return self.dir  # type: ignore[return-value]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            d = self.root / stamp
            n = 1
            while d.exists():
                n += 1
                d = self.root / f"{stamp}-{n}"
            d.mkdir(parents=True)
            self.dir = d
            meta = {"started_wall": datetime.now().isoformat(), "started_monotonic": time.monotonic(), **self.meta}
            (d / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            self._files = {
                "video": open(d / "video.bin", "wb"),
                "state": open(d / "state.jsonl", "w"),
                "cmds": open(d / "cmds.jsonl", "w"),
                "events": open(d / "events.jsonl", "w"),
            }
            self.video_bytes = 0
            self.active = True
        log.info("gravando em %s", d)
        return d

    def stop(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
            for f in self._files.values():
                f.close()
            self._files = {}
        log.info("gravação encerrada: %s", self.dir)

    def toggle(self) -> bool:
        if self.active:
            self.stop()
        else:
            self.start()
        return self.active

    def update_meta(self, **kv) -> None:
        self.meta.update(kv)
        if self.dir and (self.dir / "meta.json").exists():
            m = json.loads((self.dir / "meta.json").read_text())
            m.update(kv)
            (self.dir / "meta.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))

    # callbacks (chamados de várias threads)
    def on_video_datagram(self, t: float, data: bytes) -> None:
        if not self.active:
            return
        with self._lock:
            f = self._files.get("video")
            if f:
                f.write(VIDEO_HDR.pack(t, len(data)))
                f.write(data)
                self.video_bytes += len(data)

    def _jsonl(self, key: str, obj: dict) -> None:
        if not self.active:
            return
        with self._lock:
            f = self._files.get(key)
            if f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def on_state_raw(self, t: float, raw: str) -> None:
        self._jsonl("state", {"t": t, "raw": raw.strip()})

    def on_command(self, t: float, cmd: str) -> None:
        self._jsonl("cmds", {"t": t, "cmd": cmd})

    def on_event(self, t: float, text: str) -> None:
        self._jsonl("events", {"t": t, "text": text})

    def snapshot_dir(self) -> Path:
        d = (self.dir or self.root) / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        return d
