import math
import time

import pytest

from conftest import FakeLink
from g1_studio.core.controller import Controller
from g1_studio.core.planner import Step


def wait_idle(ctl, timeout=10):
    t0 = time.monotonic()
    while ctl.busy() and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    assert not ctl.busy()


@pytest.fixture
def events():
    return []


def make(link, events, **kw):
    return Controller(link, on_event=lambda k, d: events.append((k, d)), **kw)


def test_publishes_continuously(fake_link, events):
    ctl = make(fake_link, events)
    time.sleep(0.3)
    ctl.shutdown()
    assert len(fake_link.sent) > 15                     # ~100 Гц, даже когда стоим
    assert fake_link.sent[-1] == (0.0, 0.0, 0.0, 0.8)


def test_turn_by_imu(fake_link, events):
    ctl = make(fake_link, events)
    ctl.run([Step("turn", 90, 60)])
    wait_idle(ctl)
    ctl.shutdown()
    assert math.degrees(fake_link._yaw) == pytest.approx(90, abs=6)
    kinds = [k for k, _ in events]
    assert kinds[0] == "plan" and "step" in kinds and kinds[-1] == "done"
    assert events[-1][1]["ok"] is True


def test_turn_without_imu_warns(events):
    link = FakeLink(imu=False)
    ctl = make(link, events)
    ctl.run([Step("turn", 30, 90)])
    wait_idle(ctl)
    ctl.shutdown()
    assert any(k == "warn" and "IMU" in d["text"] for k, d in events)


def test_walk_distance_by_time(fake_link, events):
    ctl = make(fake_link, events)
    ctl.run([Step("walk", 0.5, 0.5)])
    wait_idle(ctl)
    ctl.shutdown()
    assert fake_link.x == pytest.approx(0.5, abs=0.08)  # разгон/торможение учтены во времени


def test_sim_speed_compensation(events):
    link = FakeLink(speed=0.5)                          # сим вдвое медленнее реального времени
    ctl = make(link, events, sim_speed=0.5)
    ctl.run([Step("walk", 0.4, 0.5)])
    wait_idle(ctl)
    ctl.shutdown()
    assert link.x == pytest.approx(0.4, abs=0.08)


def test_stop_cancels(fake_link, events):
    ctl = make(fake_link, events)
    ctl.run([Step("wait", 10)])
    time.sleep(0.2)
    ctl.stop()
    assert not ctl.busy()
    time.sleep(0.1)
    ctl.shutdown()
    assert events[-1] == ("done", {"ok": False, "cancelled": True})


def test_manual_overrides_plan_and_clamps(fake_link, events):
    ctl = make(fake_link, events)
    ctl.run([Step("wait", 10)])
    time.sleep(0.1)
    ctl.manual(5.0, 0, 200)                             # вне пределов
    assert not ctl.busy() and ctl.mode == "manual"
    assert ctl.target[0] == 1.0 and ctl.target[2] == pytest.approx(math.radians(90))
    ctl.manual(0, 0, 0)
    assert ctl.mode == "idle"
    ctl.shutdown()


def test_emergency_stop_zeroes_now(fake_link, events):
    ctl = make(fake_link, events)
    ctl.manual(0.8)
    time.sleep(0.4)
    assert ctl.cmd[0] > 0.3
    ctl.emergency_stop()
    assert ctl.cmd[0] == 0.0 and ctl.target[0] == 0.0
    ctl.shutdown()


def test_height_clamped(fake_link, events):
    ctl = make(fake_link, events)
    ctl.set_height(0.1)
    assert ctl.target[3] == 0.3
    ctl.shutdown()


def test_vision_without_camera(fake_link, events):
    ctl = make(fake_link, events)
    ctl.run([Step("find", 0, 0, "ball")])
    wait_idle(ctl)
    ctl.shutdown()
    assert any(k == "warn" and "камера" in d["text"] for k, d in events)
    assert events[-1][1]["ok"] is False
