"""Sala 3D sintética para o emulador (renderização por painter's algorithm com OpenCV).

Frames de mundo: x frente (da posição inicial), y esquerda, z cima (FLU), metros.
O piso e as paredes são ladrilhados com variação de cor (textura útil para VO na Fase 2),
e há "pôsteres" com fotos reais (assets do ultralytics) para o YOLO ter o que detectar.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# corpo (FLU) -> câmera (x direita, y baixo, z frente)
R_CB = np.array([[0.0, -1.0, 0.0], [0.0, 0.0, -1.0], [1.0, 0.0, 0.0]])

# Intrínsecos do "Tello simulado" (960x720). O real será calibrado.
SIM_K = np.array([[700.0, 0.0, 480.0], [0.0, 700.0, 360.0], [0.0, 0.0, 1.0]])
SIM_SIZE = (960, 720)


_NEAR = 0.02  # plano de recorte próximo (m)


def _clip_near(poly: np.ndarray, near: float) -> np.ndarray:
    """Sutherland-Hodgman de um polígono (em coordenadas de câmera) contra z >= near."""
    out = []
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        ina, inb = a[2] >= near, b[2] >= near
        if ina:
            out.append(a)
        if ina != inb:
            t = (near - a[2]) / (b[2] - a[2])
            out.append(a + t * (b - a))
    return np.array(out) if out else np.empty((0, 3))


def rot_wb(yaw: float, pitch: float = 0.0, roll: float = 0.0) -> np.ndarray:
    """Rotação corpo->mundo (FLU). yaw anti-horário +, pitch nariz p/ baixo +, roll direita p/ baixo +."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    return rz @ ry @ rx


@dataclass
class Room:
    x0: float = -1.0
    x1: float = 6.0
    y0: float = -2.5
    y1: float = 2.5
    height: float = 2.8


def _assets_dir() -> Path:
    import ultralytics

    return Path(ultralytics.__file__).parent / "assets"


class Scene:
    def __init__(self, room: Room | None = None, tile: float = 0.5, seed: int = 7) -> None:
        self.room = room or Room()
        rng = np.random.default_rng(seed)
        quads: list[np.ndarray] = []
        colors: list[np.ndarray] = []
        r = self.room

        def add(q, base, var=18):
            quads.append(np.asarray(q, dtype=np.float64))
            colors.append(np.clip(np.asarray(base) + rng.integers(-var, var + 1, 3), 0, 255))

        xs = np.arange(r.x0, r.x1 - 1e-9, tile)
        ys = np.arange(r.y0, r.y1 - 1e-9, tile)
        zs = np.arange(0.0, r.height - 1e-9, tile)
        for i, x in enumerate(xs):
            for j, y in enumerate(ys):
                x2, y2 = min(x + tile, r.x1), min(y + tile, r.y1)
                wood = (150, 110, 70) if (i + j) % 2 == 0 else (125, 90, 55)
                add([[x, y, 0], [x2, y, 0], [x2, y2, 0], [x, y2, 0]], wood)
                add([[x, y, r.height], [x, y2, r.height], [x2, y2, r.height], [x2, y, r.height]], (215, 215, 210), 6)
        for x in xs:  # paredes laterais (y0 e y1)
            for z in zs:
                x2, z2 = min(x + tile, r.x1), min(z + tile, r.height)
                add([[x, r.y0, z], [x2, r.y0, z], [x2, r.y0, z2], [x, r.y0, z2]], (190, 200, 215), 12)
                add([[x, r.y1, z], [x, r.y1, z2], [x2, r.y1, z2], [x2, r.y1, z]], (215, 200, 180), 12)
        for y in ys:  # paredes de fundo/frente (x1 e x0)
            for z in zs:
                y2, z2 = min(y + tile, r.y1), min(z + tile, r.height)
                add([[r.x1, y, z], [r.x1, y2, z], [r.x1, y2, z2], [r.x1, y, z2]], (200, 215, 195), 12)
                add([[r.x0, y, z], [r.x0, y, z2], [r.x0, y2, z2], [r.x0, y2, z]], (205, 195, 205), 12)

        n_shell = len(quads)
        # obstáculos: mesa e pilar
        for (cx, cy, sx, sy, sz, col) in ((3.0, 1.2, 0.9, 0.9, 0.75, (90, 60, 40)), (2.2, -1.3, 0.4, 0.4, r.height, (230, 230, 230))):
            self._add_box(add, cx, cy, sx, sy, sz, col)

        self.quads = np.stack(quads)               # (N, 4, 3)
        self.colors = np.stack(colors).astype(np.float64)  # (N, 3) RGB
        # Sala convexa vista de dentro: a "casca" (piso/teto/paredes) nunca oclui nada,
        # então é desenhada primeiro; só os obstáculos precisam de ordenação por profundidade.
        self.is_obstacle = np.arange(len(quads)) >= n_shell

        # pôsteres: (cantos TL, TR, BR, BL vistos de dentro da sala, imagem RGB)
        self.posters: list[tuple[np.ndarray, np.ndarray]] = []
        eps = 0.01
        assets = _assets_dir()
        bus = cv2.cvtColor(cv2.imread(str(assets / "bus.jpg")), cv2.COLOR_BGR2RGB)
        zid = cv2.cvtColor(cv2.imread(str(assets / "zidane.jpg")), cv2.COLOR_BGR2RGB)
        yc, w, zb, zt = 0.6, 1.2, 0.5, 2.1
        self.posters.append((np.array([
            [r.x1 - eps, yc + w / 2, zt], [r.x1 - eps, yc - w / 2, zt],
            [r.x1 - eps, yc - w / 2, zb], [r.x1 - eps, yc + w / 2, zb]]), bus))
        xa, xb, zb, zt = 1.8, 3.9, 0.8, 1.98
        self.posters.append((np.array([
            [xa, r.y1 - eps, zt], [xb, r.y1 - eps, zt],
            [xb, r.y1 - eps, zb], [xa, r.y1 - eps, zb]]), zid))

    @staticmethod
    def _add_box(add, cx, cy, sx, sy, sz, col):
        x0, x1, y0, y1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2
        c = np.array(col)
        add([[x0, y0, sz], [x1, y0, sz], [x1, y1, sz], [x0, y1, sz]], c + 25, 4)   # topo
        add([[x0, y0, 0], [x0, y1, 0], [x0, y1, sz], [x0, y0, sz]], c, 4)          # face -x
        add([[x1, y0, 0], [x1, y0, sz], [x1, y1, sz], [x1, y1, 0]], c - 15, 4)     # face +x
        add([[x0, y0, 0], [x0, y0, sz], [x1, y0, sz], [x1, y0, 0]], c - 25, 4)     # face -y
        add([[x0, y1, 0], [x1, y1, 0], [x1, y1, sz], [x0, y1, sz]], c - 5, 4)      # face +y

    def render(self, pos: np.ndarray, r_wb: np.ndarray, k: np.ndarray = SIM_K,
               size: tuple[int, int] = SIM_SIZE) -> np.ndarray:
        w, h = size
        fx, fy, cx, cy = k[0, 0], k[1, 1], k[0, 2], k[1, 2]
        img = np.full((h, w, 3), 35, np.uint8)
        r_cw = R_CB @ r_wb.T
        items: list[tuple[float, int, np.ndarray]] = []  # (profundidade, id, pontos 2D)

        def project(verts: np.ndarray):
            pc = (verts.reshape(-1, 3) - pos) @ r_cw.T
            return pc.reshape(verts.shape)

        vc = project(self.quads)
        z = vc[..., 2]
        ok = (z > _NEAR).all(axis=1)
        idx = np.nonzero(ok)[0]
        u = fx * vc[idx, :, 0] / z[idx] + cx
        v = fy * vc[idx, :, 1] / z[idx] + cy
        vis = ~((u.max(1) < 0) | (u.min(1) > w) | (v.max(1) < 0) | (v.min(1) > h))
        idx, u, v = idx[vis], u[vis], v[vis]
        depth = z[idx].mean(axis=1)
        for d, i, uu, vv in zip(depth, idx, u, v):
            items.append((float(d), int(i), np.stack([uu, vv], axis=1)))

        # quads que cruzam o plano próximo (ex.: piso logo à frente com o drone no chão):
        # recorta o polígono em z = NEAR em vez de descartar
        partial = np.nonzero((z > _NEAR).any(axis=1) & ~ok)[0]
        for i in partial:
            poly = _clip_near(vc[i], _NEAR)
            if len(poly) < 3:
                continue
            uv = np.stack([fx * poly[:, 0] / poly[:, 2] + cx, fy * poly[:, 1] / poly[:, 2] + cy], axis=1)
            uv = np.clip(uv, -1e5, 1e5)
            if uv[:, 0].max() < 0 or uv[:, 0].min() > w or uv[:, 1].max() < 0 or uv[:, 1].min() > h:
                continue
            items.append((float(poly[:, 2].mean()), int(i), uv))

        for pi, (corners, _) in enumerate(self.posters):
            pc = project(corners)
            if (pc[:, 2] > 0.05).all():
                uv = np.stack([fx * pc[:, 0] / pc[:, 2] + cx, fy * pc[:, 1] / pc[:, 2] + cy], axis=1)
                # leve viés para ficar à frente da parede onde está colado
                items.append((float(pc[:, 2].mean()) - 0.02, -1 - pi, uv))

        # ordem: casca -> pôsteres -> obstáculos (do mais longe ao mais perto)
        def layer(it):
            i = it[1]
            return 2 if (i >= 0 and self.is_obstacle[i]) else (1 if i < 0 else 0)
        items.sort(key=lambda it: (layer(it), -it[0]))
        for d, i, uv in items:
            if i >= 0:
                shade = float(np.clip(1.15 - 0.07 * d, 0.35, 1.0))
                col = tuple(int(c) for c in self.colors[i] * shade)
                cv2.fillConvexPoly(img, np.round(uv * 16).astype(np.int32), col, cv2.LINE_8, shift=4)
            else:
                self._draw_poster(img, self.posters[-1 - i][1], uv)
        return img

    @staticmethod
    def _draw_poster(img: np.ndarray, poster: np.ndarray, uv: np.ndarray) -> None:
        h, w = img.shape[:2]
        x0, y0 = np.floor(uv.min(0)).astype(int)
        x1, y1 = np.ceil(uv.max(0)).astype(int)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
        if x1 <= x0 or y1 <= y0:
            return
        ph, pw = poster.shape[:2]
        src = np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]])
        dst = (uv - [x0, y0]).astype(np.float32)
        hmat = cv2.getPerspectiveTransform(src, dst)
        roi = (x1 - x0, y1 - y0)
        warped = cv2.warpPerspective(poster, hmat, roi, flags=cv2.INTER_LINEAR)
        mask = cv2.warpPerspective(np.full((ph, pw), 255, np.uint8), hmat, roi, flags=cv2.INTER_NEAREST)
        region = img[y0:y1, x0:x1]
        region[mask > 0] = warped[mask > 0]
