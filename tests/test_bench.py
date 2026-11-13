"""Интеграция на MuJoCo-стенде: настоящий DDS, настоящая камера по ZMQ, YOLO, исполнитель.

    pytest -m bench           (нужны mujoco и unitree_sdk2py; ~1 мин)
"""
import math
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.bench

pytest.importorskip("mujoco")
pytest.importorskip("unitree_sdk2py")

DOMAIN, PORT = 7, 55577          # не мешать настоящему симу на domain 1 / :55555


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    log = tmp_path_factory.mktemp("bench") / "poses.json"
    p = subprocess.Popen([sys.executable, "-m", "g1_studio.bench", f"--domain={DOMAIN}", f"--port={PORT}",
                          "--ball-pos=-2.7,-2.0", f"--log={log}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    first = p.stdout.readline()
    assert "MuJoCo-стенд" in first, first
    yield p, log
    p.send_signal(2)
    p.wait(10)


@pytest.fixture(scope="module")
def studio(bench):
    from g1_studio.config import Config
    from g1_studio.core.studio import Studio
    cfg = Config()
    cfg.dds.domain, cfg.camera.port = DOMAIN, PORT
    cfg.yolo.enabled = False                     # мяч ищется по цвету, YOLO тут не нужен
    events = []
    s = Studio(cfg, on_event=lambda k, d: events.append((k, d)))
    s.start()
    t0 = time.monotonic()
    while (s.link.yaw() is None or not s.camera.alive()) and time.monotonic() - t0 < 20:
        time.sleep(0.1)
    s.events = events
    yield s
    s.shutdown()


def run_plan(s, steps, timeout=60):
    from g1_studio.core.planner import validate_plan
    _, plan, _ = validate_plan({"steps": steps})
    s.events.clear()
    s.controller.run(plan)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        done = [d for k, d in s.events if k == "done"]
        if done:
            return done[0]["ok"]
        time.sleep(0.1)
    raise TimeoutError


def test_connected(studio):
    assert studio.link.yaw() is not None
    assert studio.camera.alive()
    assert studio.camera.find("ball") is not None


def test_turn_left_90(studio):
    y0 = studio.link.yaw()
    assert run_plan(studio, [{"action": "turn", "value": 90}])
    turned = math.degrees(math.atan2(math.sin(studio.link.yaw() - y0), math.cos(studio.link.yaw() - y0)))
    assert turned == pytest.approx(90, abs=6)


def test_find_and_approach_ball(studio, bench):
    assert run_plan(studio, [{"action": "find", "value": 0, "target": "ball"},
                             {"action": "approach", "value": 0.5, "target": "ball"}])
    time.sleep(0.5)
    det = studio.camera.find("ball")
    assert det is not None and det.distance_m == pytest.approx(0.5, abs=0.12)
    assert abs(det.bearing_deg) < 12
