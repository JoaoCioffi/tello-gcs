"""Entrada manual: controle PS5 (DualSense) via SDL GameController e teclado.

Mapeamento PS5 (definido pelo piloto; ver mapping.py):
  stick esquerdo: Y = frente/trás (pitch), X = direita/esquerda (roll)
  R2 / L2: sobe / desce (proporcional)    R1 / L1: yaw direita / esquerda

Botões PS5:                         Teclado:
  △ Triângulo  decolar                T          decolar
  ✕ Xis        pousar                 L          pousar
  Create       EMERGÊNCIA (corta)     Backspace  EMERGÊNCIA (corta motores)
  D-pad ↑ / ↓  velocidade + / -       1 / 2 / 3  modo de velocidade
  □ Quadrado   foto                   P          foto
  ○ Círculo    gravar on/off          R          gravar on/off
  Options      overlay de detecção    O          overlay de detecção
                                      W/S A/D    frente/trás, esquerda/direita
                                      ↑/↓ ←/→    sobe/desce, yaw
                                      Esc        sair (pousa antes se estiver voando)

Obs.: no macOS o botão PS é capturado pelo sistema, por isso não é usado.
A app define SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1 para o controle continuar
funcionando mesmo se a janela perder o foco.
"""
from __future__ import annotations

import logging
import pygame
from pygame._sdl2 import controller as sdl_controller

from ..config import ControlConfig
from ..link.protocol import RcCommand
from .mapping import PadSample, map_pad
from .shaping import shape_axis
from .types import Action, ManualInput

log = logging.getLogger(__name__)


_PAD_BUTTONS = {
    pygame.CONTROLLER_BUTTON_Y: Action.TAKEOFF,          # Triângulo
    pygame.CONTROLLER_BUTTON_A: Action.LAND,             # Xis
    pygame.CONTROLLER_BUTTON_BACK: Action.EMERGENCY,     # Create
    pygame.CONTROLLER_BUTTON_DPAD_UP: Action.SPEED_UP,
    pygame.CONTROLLER_BUTTON_DPAD_DOWN: Action.SPEED_DOWN,
    pygame.CONTROLLER_BUTTON_X: Action.SNAPSHOT,         # Quadrado
    pygame.CONTROLLER_BUTTON_B: Action.TOGGLE_RECORD,    # Círculo
    pygame.CONTROLLER_BUTTON_START: Action.TOGGLE_DETECT,  # Options
}

_KEY_ACTIONS = {
    pygame.K_t: Action.TAKEOFF,
    pygame.K_l: Action.LAND,
    pygame.K_BACKSPACE: Action.EMERGENCY,
    pygame.K_1: Action.SPEED_SET_0,
    pygame.K_2: Action.SPEED_SET_1,
    pygame.K_3: Action.SPEED_SET_2,
    pygame.K_p: Action.SNAPSHOT,
    pygame.K_r: Action.TOGGLE_RECORD,
    pygame.K_o: Action.TOGGLE_DETECT,
    pygame.K_ESCAPE: Action.QUIT,
}

_AXIS_MAX = 32767.0


class InputManager:
    def __init__(self, cfg: ControlConfig) -> None:
        self.cfg = cfg
        self.speed_idx = cfg.default_speed_mode
        sdl_controller.init()
        self.pads: dict[int, sdl_controller.Controller] = {}
        self.joys: dict[int, pygame.joystick.JoystickType] = {}
        for i in range(pygame.joystick.get_count()):
            self._add_device(i)

    # ------------------------------------------------------------------ dispositivos
    def _add_device(self, index: int) -> None:
        try:
            joy = pygame.joystick.Joystick(index)
            iid = joy.get_instance_id()
            if iid in self.pads or iid in self.joys:
                return
            if sdl_controller.is_controller(index):
                pad = sdl_controller.Controller(index)
                self.pads[iid] = pad
                try:  # vibração de intensidade zero: faz o SDL ativar o modo estendido (bateria)
                    pad.rumble(0.0, 0.0, 1)
                except Exception:  # noqa: BLE001
                    pass
                log.info("controle conectado: %s", pad.name)
            else:
                joy.init()
                self.joys[iid] = joy
                log.warning("joystick sem mapeamento padrão: %s (só sticks)", joy.get_name())
        except pygame.error as e:
            log.warning("falha ao abrir controle %d: %s", index, e)

    def _remove_device(self, instance_id: int) -> None:
        pad = self.pads.pop(instance_id, None)
        if pad:
            log.warning("controle desconectado: %s", pad.name)
        self.joys.pop(instance_id, None)

    @property
    def device_name(self) -> str:
        if self.pads:
            return next(iter(self.pads.values())).name
        if self.joys:
            return next(iter(self.joys.values())).get_name()
        return "teclado"

    def sdl_power_level(self) -> str | None:
        """Nível grosseiro do SDL: empty/low/medium/full/wired/unknown (fallback da %)."""
        if not self.pads:
            return None
        try:
            return next(iter(self.pads.values())).as_joystick().get_power_level()
        except Exception:  # noqa: BLE001
            return None

    @property
    def speed_pct(self) -> int:
        return self.cfg.speed_modes[self.speed_idx]

    # ------------------------------------------------------------------ eventos
    def handle_event(self, ev: pygame.event.Event) -> list[Action]:
        acts: list[Action] = []
        if ev.type == pygame.CONTROLLERDEVICEADDED or ev.type == pygame.JOYDEVICEADDED:
            self._add_device(ev.device_index)
        elif ev.type == pygame.CONTROLLERDEVICEREMOVED or ev.type == pygame.JOYDEVICEREMOVED:
            self._remove_device(ev.instance_id)
        elif ev.type == pygame.CONTROLLERBUTTONDOWN:
            a = _PAD_BUTTONS.get(ev.button)
            if a:
                acts.append(a)
        elif ev.type == pygame.KEYDOWN:
            a = _KEY_ACTIONS.get(ev.key)
            if a:
                acts.append(a)
        elif ev.type == pygame.QUIT:
            acts.append(Action.QUIT)
        for a in acts:
            self._apply_speed(a)
        return acts

    def _apply_speed(self, a: Action) -> None:
        n = len(self.cfg.speed_modes)
        if a == Action.SPEED_UP:
            self.speed_idx = min(self.speed_idx + 1, n - 1)
        elif a == Action.SPEED_DOWN:
            self.speed_idx = max(self.speed_idx - 1, 0)
        elif a in (Action.SPEED_SET_0, Action.SPEED_SET_1, Action.SPEED_SET_2):
            self.speed_idx = min([Action.SPEED_SET_0, Action.SPEED_SET_1, Action.SPEED_SET_2].index(a), n - 1)

    # ------------------------------------------------------------------ amostragem
    def _pad_axes(self) -> tuple[float, float, float, float] | None:
        """Eixos já condicionados (lr, fb, ud, yaw) do controle ativo, ou None."""
        c = self.cfg
        if self.pads:
            p = next(iter(self.pads.values()))
            sample = PadSample(
                lx=p.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / _AXIS_MAX,
                ly=p.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / _AXIS_MAX,
                rx=p.get_axis(pygame.CONTROLLER_AXIS_RIGHTX) / _AXIS_MAX,
                ry=p.get_axis(pygame.CONTROLLER_AXIS_RIGHTY) / _AXIS_MAX,
                lt=p.get_axis(pygame.CONTROLLER_AXIS_TRIGGERLEFT) / _AXIS_MAX,
                rt=p.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT) / _AXIS_MAX,
                l1=bool(p.get_button(pygame.CONTROLLER_BUTTON_LEFTSHOULDER)),
                r1=bool(p.get_button(pygame.CONTROLLER_BUTTON_RIGHTSHOULDER)),
            )
            return map_pad(sample, c.deadzone, c.expo, c.trigger_deadzone, c.yaw_button_level)
        if self.joys:  # joystick genérico: só o stick esquerdo (pitch/roll)
            j = next(iter(self.joys.values()))
            if j.get_numaxes() < 2:
                return None
            return shape_axis(j.get_axis(0), c.deadzone, c.expo), shape_axis(-j.get_axis(1), c.deadzone, c.expo), 0.0, 0.0
        return None

    @staticmethod
    def _key_axes() -> tuple[float, float, float, float]:
        k = pygame.key.get_pressed()
        lr = float(k[pygame.K_d]) - float(k[pygame.K_a])
        fb = float(k[pygame.K_w]) - float(k[pygame.K_s])
        ud = float(k[pygame.K_UP]) - float(k[pygame.K_DOWN])
        yaw = float(k[pygame.K_RIGHT]) - float(k[pygame.K_LEFT])
        return lr, fb, ud, yaw

    def poll(self, now: float) -> ManualInput:
        keys = self._key_axes()
        device = "teclado"
        sticks = keys
        if not any(keys):
            pad = self._pad_axes()
            if pad is not None:
                sticks = pad
                device = self.device_name
        active = any(abs(v) > 0.0 for v in sticks)
        s = self.speed_pct
        rc = RcCommand.of(sticks[0] * s, sticks[1] * s, sticks[2] * s, sticks[3] * s)
        return ManualInput(rc, active, now, device, s, sticks)  # type: ignore[arg-type]
