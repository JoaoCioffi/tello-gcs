"""Integração: emulador UDP + driver + controlador de voo + gravação + replay (sem drone)."""
import time

import pytest

from tello_gcs.config import ControlConfig, LinkConfig, SafetyConfig
from tello_gcs.control.flight import FlightController, Phase
from tello_gcs.control.types import Action, ManualInput
from tello_gcs.link.driver import TelloLink
from tello_gcs.link.protocol import RcCommand
from tello_gcs.recorder.reader import iter_frames, iter_jsonl, iter_states
from tello_gcs.recorder.record import FlightRecorder
from tello_gcs.recorder.replay import ReplayServer
from tello_gcs.sim.emulator import EmuParams, TelloEmulator

PORTS = dict(cmd_port=28889, state_port=28890, video_port=31111)


def _wait(cond, timeout=5.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def emu():
    e = TelloEmulator("127.0.0.1", PORTS["cmd_port"], PORTS["state_port"], PORTS["video_port"],
                      params=EmuParams(takeoff_time=1.0)).start()
    yield e
    e.stop()


def _link(recorder=None):
    cfg = LinkConfig(host="127.0.0.1", **PORTS)
    return TelloLink(cfg, on_command=recorder and recorder.on_command, on_state_raw=recorder and recorder.on_state_raw,
                     on_video_datagram=recorder and recorder.on_video_datagram)


def test_full_flight_with_recording_and_replay(emu, tmp_path):
    rec = FlightRecorder(tmp_path)
    session = rec.start()
    link = _link(rec)
    assert link.connect()
    assert link.stream_on()
    assert _wait(lambda: link.video.frames.get()[2] > 10), "sem frames de vídeo"
    assert _wait(lambda: link.state_rate.hz() > 5)

    fc = FlightController(link, ControlConfig(), SafetyConfig(), on_event=rec.on_event)
    fc.start()
    try:
        fc.request(Action.TAKEOFF)
        assert _wait(lambda: fc.phase == Phase.FLYING, 6)
        x0 = emu.snapshot()["pos"][0]
        t_end = time.monotonic() + 1.2
        while time.monotonic() < t_end:  # simula a UI publicando a entrada a 60 Hz
            fc.set_manual(ManualInput(RcCommand(0, 60, 0, 0), True, time.monotonic(), "teste", 100, (0, 0.6, 0, 0)))
            time.sleep(1 / 60)
        assert emu.snapshot()["pos"][0] - x0 > 0.4, "drone não andou para frente"
        assert fc.rc_rate.hz() > 25

        # UI "trava": entrada fica velha -> o loop manda hover sozinho
        time.sleep(0.5)
        assert fc.source == "HOVER" and fc.last_rc.is_zero()
        assert emu.snapshot()["rc"] == (0, 0, 0, 0)

        fc.request(Action.LAND)
        assert _wait(lambda: fc.phase == Phase.LANDED, 6)
    finally:
        fc.stop()
        link.stream_off()
        link.close()
        rec.stop()

    frames = sum(1 for _ in iter_frames(session))
    states = list(iter_states(session))
    events = [e["text"] for e in iter_jsonl(session, "events.jsonl")]
    assert frames > 30
    assert max(s.h for _, s in states) > 50
    assert "no ar" in events and "pousado" in events

    # replay pelo mesmo pipeline
    emu.stop()
    rp = ReplayServer(session, "127.0.0.1", **PORTS, speed=2.0, loop=False).start()
    try:
        link2 = _link()
        assert link2.connect()
        assert link2.stream_on()
        assert _wait(lambda: link2.video.frames.get()[2] > 10, 5)
        assert _wait(lambda: (link2.state.get()[0] or None) is not None and link2.state.get()[0].h > 50, 8)
        link2.close()
    finally:
        rp.stop()


def test_emergency_cuts_motors(emu):
    link = _link()
    assert link.connect()
    fc = FlightController(link, ControlConfig(), SafetyConfig())
    fc.start()
    try:
        fc.request(Action.TAKEOFF)
        assert _wait(lambda: fc.phase == Phase.FLYING, 6)
        fc.request(Action.EMERGENCY)
        assert _wait(lambda: emu.snapshot()["mode"] == "landed", 1.0)
        assert fc.phase == Phase.LANDED
    finally:
        fc.stop()
        link.close()


def test_takeoff_blocked_on_low_battery():
    e = TelloEmulator("127.0.0.1", PORTS["cmd_port"], PORTS["state_port"], PORTS["video_port"],
                      params=EmuParams(battery=15)).start()
    link = _link()
    try:
        assert link.connect()
        assert _wait(lambda: link.state.get()[0] is not None)
        fc = FlightController(link, ControlConfig(), SafetyConfig(takeoff_min_battery_pct=20))
        fc.start()
        fc.request(Action.TAKEOFF)
        time.sleep(0.5)
        assert fc.phase == Phase.LANDED
        assert "takeoff" not in e.commands
        assert any("BLOQUEADA" in t for _, t in fc.events)
        fc.stop()
    finally:
        link.close()
        e.stop()


def test_video_latency_with_artificial_delay():
    """Com 150 ms de atraso no emulador, a idade do frame na chegada deve refletir isso."""
    e = TelloEmulator("127.0.0.1", PORTS["cmd_port"], PORTS["state_port"], PORTS["video_port"],
                      params=EmuParams(video_delay_ms=150)).start()
    link = _link()
    try:
        assert link.connect() and link.stream_on()
        assert _wait(lambda: link.video.frames.get()[2] > 20, 5)
        assert link.video.fps.hz() > 25  # atraso não derruba o fps
    finally:
        link.close()
        e.stop()
