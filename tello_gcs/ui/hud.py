"""HUD em pygame: vídeo + detecções à esquerda, painel de telemetria/latência à direita."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import pygame

from ..bus import RateMeter
from ..checks import CheckItem
from ..control.types import ManualInput
from ..link.protocol import RcCommand, TelloState
from ..link.video import VideoFrame
from ..perception.detector import Detections

VIDEO_W, VIDEO_H = 960, 720
PANEL_W = 340
WINDOW = (VIDEO_W + PANEL_W, VIDEO_H)

FG = (230, 230, 235)
DIM = (140, 145, 155)
OK = (80, 220, 120)
WARN = (240, 200, 60)
BAD = (240, 80, 70)
BG = (18, 20, 24)
PANEL_BG = (26, 28, 33)
ACCENT = (90, 170, 255)


@dataclass
class HudContext:
    mode: str                              # AO VIVO | SIMULADOR | REPLAY
    host: str
    frame: Optional[VideoFrame] = None
    frame_seq: int = 0
    detections: Optional[Detections] = None
    show_detections: bool = True
    state: Optional[TelloState] = None
    state_age_s: float = float("inf")
    phase: str = "-"
    source: str = "-"
    last_rc: RcCommand = field(default_factory=RcCommand)
    manual: Optional[ManualInput] = None
    warnings: list[str] = field(default_factory=list)
    events: list[tuple[float, str]] = field(default_factory=list)
    checklist: list[CheckItem] = field(default_factory=list)
    recording: bool = False
    rec_name: str = ""
    model_name: str = "?"
    metrics: dict = field(default_factory=dict)   # nomes -> valores numéricos
    banner: str = ""                              # mensagem grande central (ex.: conectando)
    pad_battery_pct: Optional[int] = None         # % do DualSense (HID)
    pad_battery_text: str = ""                    # estado ou nível grosseiro do SDL


class Hud:
    def __init__(self, screen: pygame.Surface) -> None:
        self.screen = screen
        self.font = pygame.font.SysFont("menlo", 13)
        self.font_b = pygame.font.SysFont("menlo", 13, bold=True)
        self.font_big = pygame.font.SysFont("menlo", 24, bold=True)
        self.font_small = pygame.font.SysFont("menlo", 11)
        self.fps = RateMeter()
        self._surf: Optional[pygame.Surface] = None
        self._surf_seq = -1
        self._frame_ref = None  # mantém o buffer vivo enquanto a Surface existir

    # ------------------------------------------------------------------ util
    def _t(self, text: str, pos, color=FG, font=None, surface=None) -> pygame.Rect:
        img = (font or self.font).render(text, True, color)
        return (surface or self.screen).blit(img, pos)

    def _section(self, title: str, y: int) -> int:
        x = VIDEO_W + 12
        self._t(title, (x, y), ACCENT, self.font_b)
        pygame.draw.line(self.screen, (55, 60, 70), (x, y + 17), (VIDEO_W + PANEL_W - 12, y + 17))
        return y + 22

    def _kv(self, y: int, k: str, v: str, color=FG, x_off: int = 0) -> int:
        x = VIDEO_W + 12 + x_off
        self._t(k, (x, y), DIM)
        self._t(v, (x + 110, y), color)
        return y + 16

    # ------------------------------------------------------------------ desenho
    def draw(self, c: HudContext) -> None:
        self.fps.tick()
        s = self.screen
        s.fill(BG)
        self._draw_video(c)
        if c.show_detections and c.detections is not None and c.frame is not None:
            self._draw_detections(c)
        self._draw_overlay(c)
        pygame.draw.rect(s, PANEL_BG, (VIDEO_W, 0, PANEL_W, VIDEO_H))
        self._draw_panel(c)
        if c.banner:
            self._draw_banner(c.banner)

    def _draw_video(self, c: HudContext) -> None:
        if c.frame is None:
            self._t("SEM VÍDEO", (VIDEO_W // 2 - 60, VIDEO_H // 2 - 12), DIM, self.font_big)
            return
        if c.frame_seq != self._surf_seq:
            img = c.frame.image
            surf = pygame.image.frombuffer(img.data, (img.shape[1], img.shape[0]), "RGB")
            if surf.get_size() != (VIDEO_W, VIDEO_H):
                surf = pygame.transform.smoothscale(surf, (VIDEO_W, VIDEO_H))
            self._surf, self._surf_seq, self._frame_ref = surf, c.frame_seq, img
        if self._surf is not None:
            self.screen.blit(self._surf, (0, 0))

    def _draw_detections(self, c: HudContext) -> None:
        d = c.detections
        assert d is not None and c.frame is not None
        w, h = c.frame.size
        sx, sy = VIDEO_W / w, VIDEO_H / h
        for (x0, y0, x1, y1), cls, conf, label in zip(d.boxes, d.cls, d.conf, d.labels()):
            col = _class_color(int(cls))
            r = pygame.Rect(int(x0 * sx), int(y0 * sy), int((x1 - x0) * sx), int((y1 - y0) * sy))
            pygame.draw.rect(self.screen, col, r, 2)
            txt = self.font_small.render(f"{label} {conf:.2f}", True, (10, 10, 10))
            bg = pygame.Rect(r.x, max(r.y - 15, 0), txt.get_width() + 6, 15)
            pygame.draw.rect(self.screen, col, bg)
            self.screen.blit(txt, (bg.x + 3, bg.y + 1))

    def _draw_overlay(self, c: HudContext) -> None:
        s = self.screen
        # barra superior
        bar = pygame.Surface((VIDEO_W, 30), pygame.SRCALPHA)
        bar.fill((0, 0, 0, 150))
        s.blit(bar, (0, 0))
        mode_col = {"AO VIVO": BAD, "SIMULADOR": ACCENT, "REPLAY": WARN}.get(c.mode, FG)
        x = 10
        x = self._t(c.mode, (x, 7), mode_col, self.font_b).right + 16
        phase_col = OK if c.phase == "VOANDO" else (WARN if c.phase in ("DECOLANDO", "POUSANDO") else FG)
        x = self._t(c.phase, (x, 7), phase_col, self.font_b).right + 16
        src_col = WARN if c.source == "HOVER" else (ACCENT if c.source == "AUTO" else FG)
        x = self._t(f"ctrl: {c.source}", (x, 7), src_col).right + 16
        if c.manual is not None:
            x = self._t(f"vel {c.manual.speed_pct}%  [{c.manual.device}]", (x, 7), FG).right + 16
        if c.recording and int(time.monotonic() * 2) % 2 == 0:
            pygame.draw.circle(s, BAD, (VIDEO_W - 70, 15), 6)
        if c.recording:
            self._t("REC", (VIDEO_W - 58, 7), BAD, self.font_b)

        # mira + horizonte artificial
        cx, cy = VIDEO_W // 2, VIDEO_H // 2
        pygame.draw.line(s, (255, 255, 255), (cx - 12, cy), (cx + 12, cy), 1)
        pygame.draw.line(s, (255, 255, 255), (cx, cy - 12), (cx, cy + 12), 1)
        if c.state is not None:
            roll = math.radians(c.state.roll)
            off = c.state.pitch * 4
            dx, dy = math.cos(roll) * 90, math.sin(roll) * 90
            pygame.draw.line(s, OK, (cx - dx, cy + off - dy), (cx - 30 * math.cos(roll), cy + off - 30 * math.sin(roll)), 2)
            pygame.draw.line(s, OK, (cx + 30 * math.cos(roll), cy + off + 30 * math.sin(roll)), (cx + dx, cy + off + dy), 2)

        # ajuda (só no chão, para não poluir o voo)
        if c.phase != "VOANDO":
            lines = [
                "PS5:  Tri decolar | X pousar | Create EMERGENCIA | D-pad cima/baixo velocidade",
                "      stick E: frente/tras + roll | R2/L2 sobe/desce | R1/L1 yaw dir/esq",
                "      Quadrado foto | Circulo grava | Options deteccao",
                "Tecl: T decolar | L pousar | Backspace EMERGENCIA | 1/2/3 velocidade",
                "      WASD frente/lado | setas sobe/desce/yaw | P foto | R grava | Esc sai",
            ]
            hb = pygame.Surface((620, 14 * len(lines) + 10), pygame.SRCALPHA)
            hb.fill((0, 0, 0, 140))
            hy = VIDEO_H - hb.get_height() - (56 if c.warnings else 8)
            s.blit(hb, (8, hy))
            for i, line in enumerate(lines):
                self._t(line, (14, hy + 5 + 14 * i), (210, 210, 215), self.font_small)

        # alertas
        if c.warnings:
            txt = "  |  ".join(c.warnings)
            img = self.font_b.render(txt, True, (255, 255, 255))
            box = pygame.Surface((img.get_width() + 20, 28), pygame.SRCALPHA)
            box.fill((200, 40, 40, 210))
            s.blit(box, (VIDEO_W // 2 - box.get_width() // 2, VIDEO_H - 44))
            s.blit(img, (VIDEO_W // 2 - img.get_width() // 2, VIDEO_H - 38))

    def _draw_banner(self, text: str) -> None:
        img = self.font_big.render(text, True, (255, 255, 255))
        box = pygame.Surface((img.get_width() + 40, img.get_height() + 24), pygame.SRCALPHA)
        box.fill((0, 0, 0, 200))
        x = VIDEO_W // 2 - box.get_width() // 2
        y = VIDEO_H // 2 - box.get_height() // 2 - 60
        self.screen.blit(box, (x, y))
        self.screen.blit(img, (x + 20, y + 12))

    def _draw_panel(self, c: HudContext) -> None:
        m = c.metrics
        st = c.state
        y = 8
        y = self._section("LINK", y)
        link_col = OK if c.state_age_s < 1.0 else BAD
        y = self._kv(y, "host", c.host, link_col)
        y = self._kv(y, "modelo", c.model_name)
        y = self._kv(y, "telemetria", f"{m.get('state_hz', 0):5.1f} Hz", _rate_color(m.get("state_hz", 0), 8, 5))
        y = self._kv(y, "vídeo", f"{m.get('video_fps', 0):5.1f} fps  err {m.get('decode_err', 0)}",
                     _rate_color(m.get("video_fps", 0), 25, 15))
        y = self._kv(y, "rc enviado", f"{m.get('rc_hz', 0):5.1f} Hz")
        rtt = m.get("rtt_ms")
        y = self._kv(y, "RTT cmd", f"{rtt:.0f} ms" if rtt is not None else "-")

        y = self._section("VOO", y + 4)
        if st is not None:
            bat_col = OK if st.bat > 40 else (WARN if st.bat > 20 else BAD)
            y = self._kv(y, "bateria", f"{st.bat}%", bat_col)
            pygame.draw.rect(self.screen, (60, 60, 60), (VIDEO_W + 180, y - 13, 140, 9))
            pygame.draw.rect(self.screen, bat_col, (VIDEO_W + 180, y - 13, int(1.4 * max(0, min(100, st.bat))), 9))
            tcol = OK if st.temph < 75 else (WARN if st.temph < 85 else BAD)
            y = self._kv(y, "temperatura", f"{st.templ}-{st.temph} °C", tcol)
            y = self._kv(y, "altura h/tof", f"{st.h} cm / {st.tof if st.tof_valid else '--'} cm")
            y = self._kv(y, "atitude p/r/y", f"{st.pitch:+4d} {st.roll:+4d} {st.yaw:+5d}°")
            y = self._kv(y, "vel x/y/z", f"{st.vgx:+3d} {st.vgy:+3d} {st.vgz:+3d} dm/s")
            y = self._kv(y, "motor", f"{st.time} s")
        else:
            y = self._kv(y, "estado", "sem telemetria", BAD)

        y = self._section("LATÊNCIA (lado Mac)", y + 4)
        age = m.get("frame_age_ms")
        y = self._kv(y, "frame->tela", f"{age:5.0f} ms" if age is not None else "-",
                     OK if (age or 999) < 60 else WARN)
        dec = m.get("decode_ms")
        y = self._kv(y, "decode", f"{dec:5.1f} ms" if dec is not None else "-")
        inf = m.get("infer_ms")
        y = self._kv(y, "detector", f"{inf:5.1f} ms  {m.get('det_hz', 0):4.1f} Hz" if inf is not None else "carregando...")
        y = self._kv(y, "HUD", f"{self.fps.hz():5.1f} fps")

        y = self._section("CONTROLE", y + 4)
        r = c.last_rc
        y = self._kv(y, "rc lr/fb/ud/yw", f"{r.lr:+4d} {r.fb:+4d} {r.ud:+4d} {r.yaw:+4d}")
        if c.pad_battery_pct is not None:
            p = c.pad_battery_pct
            pcol = OK if p > 40 else (WARN if p > 20 else BAD)
            if "carregando" in c.pad_battery_text or "completa" in c.pad_battery_text:
                pcol = ACCENT
            y = self._kv(y, "bateria ctrl", f"{p:3d}%  {c.pad_battery_text}", pcol)
        elif c.pad_battery_text:
            y = self._kv(y, "bateria ctrl", c.pad_battery_text, DIM)
        y = self._draw_sticks(c, y + 2)

        y = self._section("CHECKLIST", y + 4)
        for it in c.checklist:
            col = OK if it.status is True else (BAD if it.status is False else WARN)
            pygame.draw.circle(self.screen, col, (VIDEO_W + 18, y + 7), 4)
            self._t(f"{it.label}", (VIDEO_W + 28, y), FG)
            self._t(it.detail[:22], (VIDEO_W + 150, y), DIM)
            y += 15

        y = self._section("EVENTOS", y + 4)
        now = time.monotonic()
        for t, text in c.events[-5:]:
            self._t(f"[{now - t:4.0f}s] {text}"[:46], (VIDEO_W + 12, y), FG, self.font_small)
            y += 13


    def _draw_sticks(self, c: HudContext, y: int) -> int:
        size = 56
        sticks = c.manual.sticks if c.manual else (0.0, 0.0, 0.0, 0.0)
        lr, fb, ud, yaw = sticks
        for i, (sx, sy, label) in enumerate(((lr, fb, "stick E"), (yaw, ud, "yaw/alt"))):
            x0 = VIDEO_W + 40 + i * 140
            rect = pygame.Rect(x0, y, size, size)
            pygame.draw.rect(self.screen, (60, 64, 72), rect, 1)
            pygame.draw.line(self.screen, (50, 54, 60), (rect.centerx, rect.top), (rect.centerx, rect.bottom))
            pygame.draw.line(self.screen, (50, 54, 60), (rect.left, rect.centery), (rect.right, rect.centery))
            px = rect.centerx + int(sx * size / 2)
            py = rect.centery - int(sy * size / 2)
            pygame.draw.circle(self.screen, ACCENT, (px, py), 5)
            self._t(label, (x0 + size + 6, y + size // 2 - 7), DIM, self.font_small)
        return y + size + 4


def _rate_color(v: float, good: float, bad: float):
    return OK if v >= good else (WARN if v >= bad else BAD)


def _class_color(cls: int) -> tuple[int, int, int]:
    palette = [(255, 99, 71), (60, 180, 255), (120, 230, 120), (255, 200, 60), (200, 120, 255),
               (255, 140, 200), (80, 220, 210), (240, 240, 120)]
    return palette[cls % len(palette)]
