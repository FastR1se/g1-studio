"""MuJoCo-стенд вместо Isaac Sim — чтобы проверить G1 Studio за секунды, без Isaac.

Говорит с приложением ровно так же, как sim_main.py:
  * команды — DDS rt/run_command/cmd ("[vx, vy, wz, height]"), сбрасываются после каждого шага;
  * ориентация — DDS rt/lowstate (кватернион xyzw, как в симе Unitree);
  * головная камера — JPEG по ZMQ на tcp://*:55555, та же геометрия, что у D435 на G1.

Робот здесь — капсула, которая «идёт» с заданной скоростью (без настоящей походки),
мяч — настоящее физическое тело: его можно толкнуть.

  pip install mujoco          # один раз (или pip install -e .[bench])
  python -m g1_studio.bench   # или «Что запускать: bench» в настройках G1 Studio
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import signal
import threading
import time

ROBOT_START = (-3.9, -2.818)           # где робот стоит в сцене Isaac — пересчитываем позицию мяча
PITCH = 0.8307767
FOVY = math.degrees(2 * math.atan(240 / 243.2))       # 89.2° — как у камеры сима
TORSO_H = 0.83
CAM = (0.0576235, 0.01753, TORSO_H + 0.41987)


def scene_xml(ball_xy=(1.2, 0.818), distractors=True) -> str:
    s, c = math.sin(PITCH), math.cos(PITCH)
    dis = ""
    if distractors:
        dis = """
    <body pos="2.6 -1.5 0"><geom type="box" size="0.3 0.6 0.4" pos="0 0 0.4" rgba="0.45 0.35 0.25 1"/>
      <geom type="cylinder" size="0.018 0.175" pos="0 0.2 0.975" rgba="0.15 0.15 0.15 1"/></body>
    <geom name="wall" type="box" size="0.05 4 1.5" pos="4.5 0 1.5" rgba="0.7 0.7 0.72 1"/>
    <geom name="redsign" type="box" size="0.06 0.25 0.25" pos="4.4 1.0 1.6" rgba="0.85 0.05 0.05 1"/>
    <geom name="cone" type="cylinder" size="0.12 0.2" pos="1.8 -1.2 0.2" rgba="1.0 0.45 0.0 1"/>
    <geom name="stripe" type="box" size="1.2 0.03 0.002" pos="2.0 1.6 0.002" rgba="0.85 0.05 0.05 1"/>"""
    return f"""<mujoco>
  <option timestep="0.002" gravity="0 0 -9.81"/>
  <visual><global offwidth="640" offheight="480"/><quality shadowsize="2048"/></visual>
  <asset>
    <texture name="grid" type="2d" builtin="checker" rgb1="0.42 0.42 0.42" rgb2="0.36 0.36 0.37" width="512" height="512"/>
    <material name="floor" texture="grid" texrepeat="8 8" reflectance="0.05"/>
  </asset>
  <worldbody>
    <light pos="0 0 6" dir="0 0 -1" diffuse="0.8 0.8 0.8" directional="true"/>
    <light pos="2 2 4" dir="-0.3 -0.3 -1" diffuse="0.3 0.3 0.3"/>
    <geom name="floor" type="plane" size="10 10 0.1" material="floor" friction="0.8 0.005 0.0001"/>
    <body name="robot" pos="0 0 0" mocap="true">
      <geom type="capsule" fromto="0 0 0.25 0 0 1.2" size="0.13" rgba="0.6 0.6 0.65 1"/>
      <geom name="feet" type="box" size="0.13 0.16 0.04" pos="0.02 0 0.04" rgba="0.3 0.3 0.3 1"/>
      <camera name="head" pos="{CAM[0]} {CAM[1]} {CAM[2]}" xyaxes="0 -1 0 {s} 0 {c}" fovy="{FOVY}"/>
    </body>
    <body name="ball" pos="{ball_xy[0]} {ball_xy[1]} 0.1">
      <joint type="free" damping="0.12"/>
      <geom name="ball" type="sphere" size="0.1" mass="0.4" rgba="0.9 0.05 0.05 1" friction="0.8 0.01 0.001" solref="0.01 1"/>
    </body>
    {dis}
  </worldbody>
</mujoco>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MuJoCo-стенд для G1 Studio (вместо Isaac Sim)")
    ap.add_argument("--ball-pos", default="-2.7,-2.0", help="мяч x,y в координатах сцены Isaac")
    ap.add_argument("--domain", type=int, default=1)
    ap.add_argument("--port", type=int, default=55555)
    ap.add_argument("--speed", type=float, default=float(os.environ.get("SIM_SPEED", "1")),
                    help="во сколько раз медленнее реального времени идти (0.6 — как слабый Isaac)")
    ap.add_argument("--fps", type=float, default=15.0, help="кадров камеры в секунду")
    ap.add_argument("--duration", type=float, default=0.0, help="сколько секунд работать (0 — пока не остановят)")
    ap.add_argument("--no-distractors", action="store_true", help="без помех (стена, конус, красная полоса)")
    ap.add_argument("--log", default="", help="записать позы робота и мяча в JSON при выходе")
    a = ap.parse_args(argv)

    os.environ.setdefault("MUJOCO_GL", "egl")
    import cv2
    import mujoco
    import numpy as np
    import zmq
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
    from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    bx, by = (float(v) for v in a.ball_pos.split(","))
    m = mujoco.MjModel.from_xml_string(scene_xml((bx - ROBOT_START[0], by - ROBOT_START[1]),
                                                 not a.no_distractors))
    d = mujoco.MjData(m)
    try:
        ren = mujoco.Renderer(m, 480, 640)
    except Exception as e:
        print(f"✗ MuJoCo не может рисовать ({e}). Попробуй MUJOCO_GL=osmesa или glfw.", flush=True)
        return 1
    ball_q = m.jnt_qposadr[m.body_jntadr[m.body("ball").id]]
    mocap = m.body_mocapid[m.body("robot").id]

    ChannelFactoryInitialize(a.domain)
    latest = {"cmd": None}
    lock = threading.Lock()

    def on_cmd(msg):
        with lock:
            latest["cmd"] = msg.data

    sub = ChannelSubscriber("rt/run_command/cmd", String_)
    sub.Init(on_cmd, 1)                                        # как у Unitree: очередь 1
    pub = ChannelPublisher("rt/lowstate", LowState_)
    pub.Init()
    ls = unitree_hg_msg_dds__LowState_()
    zs = zmq.Context().socket(zmq.PUB)
    zs.setsockopt(zmq.SNDHWM, 1)
    zs.bind(f"tcp://*:{a.port}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    x = y = yaw = 0.0
    vx = vy = wz = 0.0
    h, phase = 0.8, 0.0
    tau, policy_dt, phys_dt = 0.25, 0.02, m.opt.timestep
    log = []
    t_start = t_prev = time.time()
    last_render = last_print = 0.0
    print(f"✓ MuJoCo-стенд: DDS domain {a.domain}, камера tcp://*:{a.port}, скорость {a.speed}×", flush=True)
    while not stop.is_set() and (a.duration <= 0 or time.time() - t_start < a.duration):
        now = time.time()
        dt = min(now - t_prev, 0.1) * a.speed
        t_prev = now
        c = [0.0, 0.0, 0.0, 0.8]                               # как в Isaac: команда живёт один шаг
        with lock:
            if latest["cmd"] is not None:
                try:
                    c = [float(v) for v in ast.literal_eval(latest["cmd"])]
                except (ValueError, SyntaxError):
                    pass
                latest["cmd"] = None
        k = min(dt / tau, 1.0)
        vx += (c[0] - vx) * k
        vy += (c[1] - vy) * k
        wz += (c[2] - wz) * k
        h += (c[3] - h) * k
        speed = math.hypot(vx, vy) + 0.3 * abs(wz)
        phase += 2 * math.pi * 1.8 * dt
        bob = min(speed / 0.3, 1.0)                            # покачивание при ходьбе — как у живой камеры
        pitch = math.radians(2.0) * bob * math.sin(phase)
        dz = 0.015 * bob * math.sin(2 * phase)
        droll = math.radians(1.5) * bob * math.sin(phase + 1)
        x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
        y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
        yaw += wz * dt
        d.mocap_pos[mocap] = (x, y, (h - 0.8) + dz)
        q = np.zeros(4)
        mujoco.mju_euler2Quat(q, np.array([droll, pitch, yaw]), "xyz")
        d.mocap_quat[mocap] = q
        mujoco.mj_step(m, d, nstep=max(1, round(dt / phys_dt)))
        ls.imu_state.quaternion[:] = [0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)]   # xyzw
        pub.Write(ls)
        if now - last_render >= 1 / a.fps:
            last_render = now
            ren.update_scene(d, camera="head")
            ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(ren.render(), cv2.COLOR_RGB2BGR))
            if ok:
                zs.send(jpg.tobytes())
        bxx, byy = float(d.qpos[ball_q]), float(d.qpos[ball_q + 1])
        if a.log:
            log.append([round(now, 3), round(x, 4), round(y, 4), round(math.degrees(yaw), 2),
                        round(bxx, 4), round(byy, 4)])
        if now - last_print > 10:
            last_print = now
            print(f"робот ({x:+.2f}, {y:+.2f}) курс {math.degrees(yaw):+.0f}°, мяч ({bxx:+.2f}, {byy:+.2f})", flush=True)
        time.sleep(max(0.0, policy_dt - (time.time() - now)))  # не чаще 50 Гц
    if a.log:
        with open(a.log, "w") as f:
            json.dump({"log": log}, f)
    zs.close(0)
    print("MuJoCo-стенд остановлен", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
