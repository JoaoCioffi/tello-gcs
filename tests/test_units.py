import math

import numpy as np
import pytest

from tello_gcs.config import SafetyConfig
from tello_gcs.control.arbiter import Arbiter
from tello_gcs.control.failsafe import Failsafe
from tello_gcs.control.script import ScriptedInput
from tello_gcs.control.shaping import apply_deadzone, shape_axis
from tello_gcs.control.types import Action
from tello_gcs.link.protocol import RcCommand, parse_state
from tello_gcs.link.video import H264Assembler
from tello_gcs.perception.latency import estimate_latency

STATE_V2 = "pitch:1;roll:-2;yaw:45;vgx:3;vgy:0;vgz:-1;templ:60;temph:63;tof:85;h:80;bat:76;baro:121.34;time:12;agx:5.00;agy:-3.00;agz:-998.00;\r\n"
STATE_V3 = "mid:-1;x:-100;y:-100;z:-100;mpry:0,0,0;" + STATE_V2


# ---------------------------------------------------------------- protocolo
def test_parse_state_sdk2():
    s = parse_state(STATE_V2)
    assert (s.pitch, s.roll, s.yaw, s.h, s.tof, s.bat, s.temph) == (1, -2, 45, 80, 85, 76, 63)
    assert s.baro == pytest.approx(121.34) and s.agz == pytest.approx(-998.0)
    assert s.extra == {}
    assert s.tof_valid


def test_parse_state_sdk3_keeps_extras():
    s = parse_state(STATE_V3)
    assert s.bat == 76 and s.extra["mid"] == "-1" and s.extra["mpry"] == "0,0,0"


def test_parse_state_tolerates_garbage():
    s = parse_state("bat:abc;h:10;lixo;;")
    assert s.h == 10 and s.extra["bat"] == "abc"


def test_rc_clamp_and_format():
    assert RcCommand.of(150, -150, 33.6, 0).to_cmd() == "rc 100 -100 34 0"
    assert RcCommand().is_zero()


# ---------------------------------------------------------------- sticks
def test_deadzone_and_expo():
    assert apply_deadzone(0.05, 0.08) == 0.0
    assert apply_deadzone(1.0, 0.08) == 1.0
    assert apply_deadzone(-1.0, 0.08) == -1.0
    assert shape_axis(0.5, 0.0, 0.0) == pytest.approx(0.5)
    assert abs(shape_axis(0.5, 0.0, 0.5)) < 0.5      # expo reduz sensibilidade no centro
    assert shape_axis(1.2, 0.08, 0.3) == pytest.approx(1.0)


# ---------------------------------------------------------------- árbitro
def test_manual_overrides_auto_and_holds():
    arb = Arbiter(override_hold_s=1.0)
    auto = RcCommand(0, 20, 0, 0)
    assert arb.decide(RcCommand(), False, auto, 0.0) == (auto, "AUTO")
    man = RcCommand(10, 0, 0, 0)
    assert arb.decide(man, True, auto, 1.0) == (man, "MANUAL")
    # soltou o stick: autonomia continua pausada por 1 s
    assert arb.decide(RcCommand(), False, auto, 1.5)[1] == "MANUAL"
    assert arb.decide(RcCommand(), False, auto, 2.1) == (auto, "AUTO")


# ---------------------------------------------------------------- failsafe
def _st(**kw):
    base = dict(bat=80, h=80, temph=60, tof=80)
    base.update(kw)
    return parse_state(";".join(f"{k}:{v}" for k, v in base.items()))


def test_link_lost_forces_hover():
    fs = Failsafe(SafetyConfig())
    d = fs.evaluate(_st(), 2.0, True, RcCommand(0, 50, 0, 0))
    assert d.rc.is_zero() and any("LINK" in w for w in d.warnings)


def test_critical_battery_lands_once():
    fs = Failsafe(SafetyConfig(land_battery_pct=12))
    d1 = fs.evaluate(_st(bat=11), 0.1, True, RcCommand())
    d2 = fs.evaluate(_st(bat=11), 0.1, True, RcCommand())
    assert d1.actions == ["land"] and d2.actions == []


def test_ceiling_blocks_climb_and_pushes_down():
    fs = Failsafe(SafetyConfig(max_height_cm=200))
    assert fs.evaluate(_st(h=205), 0.1, True, RcCommand(0, 0, 60, 0)).rc.ud == 0
    assert fs.evaluate(_st(h=205), 0.1, True, RcCommand(0, 0, -40, 0)).rc.ud == -40
    assert fs.evaluate(_st(h=240), 0.1, True, RcCommand(0, 0, 60, 0)).rc.ud == -30


def test_stale_input_hover():
    fs = Failsafe(SafetyConfig())
    d = fs.evaluate(_st(), 0.1, True, RcCommand(0, 50, 0, 0), input_stale=True)
    assert d.rc.is_zero()


def test_takeoff_gate():
    fs = Failsafe(SafetyConfig(takeoff_min_battery_pct=20))
    assert fs.can_takeoff(_st(bat=19), 0.1)[0] is False
    assert fs.can_takeoff(_st(bat=50), 0.1)[0] is True
    assert fs.can_takeoff(None, 0.1)[0] is False


# ---------------------------------------------------------------- script
def test_scripted_input():
    s = ScriptedInput("1:takeoff, 2:rc 0 40 0 0, 3:land", t0=100.0)
    assert s.pop_actions(100.5) == []
    assert s.pop_actions(101.0) == [Action.TAKEOFF]
    s.pop_actions(102.0)
    assert s.poll(102.0).rc == RcCommand(0, 40, 0, 0) and s.poll(102.0).active
    assert s.pop_actions(103.0) == [Action.LAND]


# ---------------------------------------------------------------- vídeo
def test_assembler_splits_on_short_datagram():
    a = H264Assembler()
    frame1 = b"\x00\x00\x00\x01" + b"a" * 3000
    chunks = [frame1[i:i + 1460] for i in range(0, len(frame1), 1460)]
    out = []
    for c in chunks:
        out += a.push(c)
    # no 1º frame o MTU ainda está sendo aprendido, mas o último chunk é menor -> fecha
    assert out == [frame1]
    frame2 = b"\x00\x00\x00\x01" + b"b" * 1456  # exatamente 1460 B (múltiplo do MTU)
    assert a.push(frame2) == []
    frame3 = b"\x00\x00\x00\x01" + b"c" * 100
    # o start code do próximo frame fecha o anterior; o curto fecha a si mesmo
    assert a.push(frame3) == [frame2, frame3]


# ---------------------------------------------------------------- latência
def test_latency_estimation_synthetic():
    true_lat = 0.160
    toggles = [(1.0 + i, i % 2 == 0) for i in range(8)]   # branco, preto, branco...
    samples = []
    t = 0.5
    while t < 9.2:
        last = [w for tt, w in toggles if tt <= t - true_lat]
        bright = 230.0 if (last and last[-1]) else 20.0
        samples.append((t, bright))
        t += 1 / 30
    r = estimate_latency(toggles, samples)
    assert r["n"] >= 6
    assert r["median_ms"] == pytest.approx(true_lat * 1000, abs=35)


def test_latency_low_contrast_reports_error():
    r = estimate_latency([(1.0, True), (2.0, False)], [(i / 30, 100.0) for i in range(100)])
    assert "error" in r


# ---------------------------------------------------------------- calibração
def test_calibration_recovers_focal_length():
    import cv2

    from tello_gcs.perception.calibration import calibrate, select_views

    k_true = np.array([[700.0, 0, 480], [0, 700.0, 360], [0, 0, 1]])
    px, margin, sq = 40, 40, 0.03
    target = np.full((7 * px + 2 * margin, 10 * px + 2 * margin), 255, np.uint8)
    for r in range(7):
        for c in range(10):
            if (r + c) % 2 == 0:
                target[margin + r * px: margin + (r + 1) * px, margin + c * px: margin + (c + 1) * px] = 0
    a = np.array([[sq / px, 0, -margin * sq / px], [0, sq / px, -margin * sq / px], [0, 0, 1]])
    rng = np.random.default_rng(0)
    images = []
    for _ in range(30):
        rvec = rng.uniform(-0.45, 0.45, 3)
        rvec[2] = rng.uniform(-0.3, 0.3)
        rmat, _ = cv2.Rodrigues(rvec)
        t = np.array([rng.uniform(-0.15, 0.0), rng.uniform(-0.12, 0.0), rng.uniform(0.45, 0.8)])
        h = k_true @ np.column_stack([rmat[:, 0], rmat[:, 1], t]) @ a
        img = cv2.warpPerspective(target, h, (960, 720), borderValue=128)
        images.append(cv2.cvtColor(img, cv2.COLOR_GRAY2RGB))
    views, size = select_views(images, 9, 6)
    cal = calibrate(views, size, 9, 6, sq)
    assert cal.rms < 1.0
    assert cal.k[0, 0] == pytest.approx(700, rel=0.03)
    assert cal.k[0, 2] == pytest.approx(480, abs=20)
    assert math.isfinite(cal.dist[0])


# ---------------------------------------------------------------- mapeamento PS5
def _pad(**kw):
    from tello_gcs.control.mapping import PadSample

    base = dict(lx=0.0, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0, l1=False, r1=False)
    base.update(kw)
    return PadSample(**base)


def _map(**kw):
    from tello_gcs.control.mapping import map_pad

    return map_pad(_pad(**kw), deadzone=0.08, expo=0.0, trigger_deadzone=0.05, yaw_button_level=0.8)


def test_pad_left_stick_pitch_and_roll():
    lr, fb, ud, yaw = _map(ly=-1.0)          # stick esquerdo para frente (SDL: cima = negativo)
    assert fb == pytest.approx(1.0) and lr == 0 and ud == 0 and yaw == 0
    assert _map(ly=1.0)[1] == pytest.approx(-1.0)   # para trás
    assert _map(lx=1.0)[0] == pytest.approx(1.0)    # roll direita
    assert _map(lx=-1.0)[0] == pytest.approx(-1.0)  # roll esquerda


def test_pad_triggers_throttle():
    assert _map(rt=1.0)[2] == pytest.approx(1.0)    # R2 sobe
    assert _map(lt=1.0)[2] == pytest.approx(-1.0)   # L2 desce
    assert _map(lt=0.03, rt=0.03)[2] == 0.0         # repouso dentro da zona morta
    assert 0.4 < _map(rt=0.5)[2] < 0.5              # proporcional


def test_pad_shoulders_yaw():
    assert _map(r1=True)[3] == pytest.approx(0.8)   # R1 yaw direita
    assert _map(l1=True)[3] == pytest.approx(-0.8)  # L1 yaw esquerda
    assert _map(l1=True, r1=True)[3] == 0.0


def test_pad_right_stick_unused():
    assert _map(rx=1.0, ry=-1.0) == (0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------- bateria do DualSense
def test_parse_dualsense_battery():
    from tello_gcs.control.pad_battery import parse_battery

    bt = [0] * 78
    bt[0], bt[54] = 0x31, 0x08            # BT estendido: nível 8 -> 85%, descarregando
    assert parse_battery(bt) == (85, "descarregando")
    bt[54] = 0x13                          # carregando, nível 3 -> 35%
    assert parse_battery(bt) == (35, "carregando")
    bt[54] = 0x2A                          # carga completa
    assert parse_battery(bt) == (100, "carga completa")
    usb = [0] * 64
    usb[0], usb[53] = 0x01, 0x0A           # USB: nível 10 -> 100%
    assert parse_battery(usb) == (100, "descarregando")
    assert parse_battery([0x01] + [0] * 9) is None  # BT básico não tem bateria
    assert parse_battery([]) is None
