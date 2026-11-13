"""Команда g1-studio.

  g1-studio                 окно (по умолчанию)
  g1-studio --dry           окно без сима: проверить интерфейс и планы Qwen
  g1-studio chat            чат с Qwen в терминале
  g1-studio vision          что видит робот: окно OpenCV или кадры в папку (--save)
  g1-studio scene ...       мяч и предметы в сцене: status | ball on/off | objects on/off | find
  g1-studio sim             запустить сим в этом терминале (по настройкам); --print — только показать команду
  g1-studio bench           MuJoCo-стенд вместо Isaac Sim (нужен pip install mujoco)
  g1-studio doctor          проверить, всё ли на месте
  g1-studio config          где файл настроек; --show — показать его
"""
from __future__ import annotations

import argparse
import logging
import math
import shlex
import sys
import time

from g1_studio import APP_NAME, __version__, logs
from g1_studio.config import Config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="g1-studio", description=f"{APP_NAME} — пульт для Unitree G1 в Isaac Sim",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    p.add_argument("--config", help="другой файл настроек (по умолчанию ~/.config/g1-studio/config.toml)")
    p.add_argument("--dry", action="store_true", help="без сима: команды никуда не отправляются")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог в терминал")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="окно (по умолчанию)")
    sub.add_parser("chat", help="чат с Qwen в терминале")
    v = sub.add_parser("vision", help="что видит робот")
    v.add_argument("--save", help="папка: сохранять размеченные кадры раз в секунду (без окна)")
    v.add_argument("--no-yolo", action="store_true", help="только мяч")
    s = sub.add_parser("scene", help="мяч и предметы в сцене")
    s.add_argument("what", choices=["status", "ball", "objects", "find"])
    s.add_argument("state", nargs="?", choices=["on", "off"])
    sm = sub.add_parser("sim", help="запустить сим в этом терминале")
    sm.add_argument("--print", action="store_true", help="только показать команду")
    b = sub.add_parser("bench", help="MuJoCo-стенд вместо Isaac Sim")
    b.add_argument("rest", nargs=argparse.REMAINDER, help="аргументы стенда (--help — список)")
    sub.add_parser("doctor", help="проверить установку")
    c = sub.add_parser("config", help="файл настроек")
    c.add_argument("--show", action="store_true", help="показать содержимое")
    a = p.parse_args(argv)

    cmd = a.cmd or "gui"
    logs.setup(verbose=a.verbose, console=cmd != "gui" or a.verbose)
    cfg = Config.load(a.config)

    if cmd == "gui":
        from g1_studio.gui.app import run
        return run(cfg, dry=a.dry)
    if cmd == "chat":
        return chat(cfg, a.dry)
    if cmd == "vision":
        return vision(cfg, a.save, a.no_yolo)
    if cmd == "scene":
        return scene_cmd(cfg, a.what, a.state)
    if cmd == "sim":
        return sim_cmd(cfg, a.print)
    if cmd == "bench":
        from g1_studio import bench
        return bench.main(a.rest)
    if cmd == "doctor":
        from g1_studio.doctor import run as doctor
        return doctor(cfg)
    if cmd == "config":
        print(cfg.path)
        if a.show:
            print(cfg.path.read_text(encoding="utf-8") if cfg.path.is_file() else cfg.to_toml())
        return 0
    return 2


# ----------------------------------------------------------------------------
def chat(cfg: Config, dry: bool) -> int:
    from g1_studio.core.planner import EXIT_WORDS
    from g1_studio.core.studio import Studio

    def on_event(kind, data):
        if kind == "reply":
            print(f"qwen ({data['seconds']:.1f} с)> {data['text'] or '(без ответа)'}", flush=True)
            for w in data["warnings"]:
                print(f"  ! {w}", flush=True)
        elif kind == "step":
            print(f"  [{data['i']}/{data['n']}] {data['text']}", flush=True)
        elif kind in ("info", "warn"):
            print(("    " if kind == "info" else "  ! ") + data["text"], flush=True)
        elif kind == "done" and data["ok"]:
            print("  ✓ готово", flush=True)

    studio = Studio(cfg, dry=dry, on_event=on_event)
    studio.start()
    if studio.link_note:
        print(f"! {studio.link_note}")
    for _ in range(30):
        if studio.link.yaw() is not None:
            break
        time.sleep(0.1)
    yaw = studio.link.yaw()
    print(f"✓ Связь с симом есть, курс {math.degrees(yaw):.0f}°" if yaw is not None
          else "! Не вижу rt/lowstate: сим запущен? Повороты будут по времени, а не по IMU.")
    if studio.yolo_note:
        print(("✓ " if studio.camera and studio.camera.yolo else "! ") + studio.yolo_note)
    print(f"Qwen: {cfg.qwen.model}. Пиши команды по-русски. «стоп» — остановка, «q» — выход.")
    try:
        while True:
            try:
                text = input("ты> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                break
            res = studio.command(text)
            if res.stopped:
                print("■ стоп")
            elif res.error:
                print(f"✗ Qwen: {res.error}")
    except KeyboardInterrupt:
        print()
    finally:
        studio.shutdown()
        print("Остановил робота, выхожу.")
    return 0


def vision(cfg: Config, save: str | None, no_yolo: bool) -> int:
    import os

    import cv2

    from g1_studio.vision.detect import annotate
    from g1_studio.vision.stream import CameraStream, load_yolo

    yolo = None
    if cfg.yolo.enabled and not no_yolo:
        yolo, msg = load_yolo(cfg.yolo.model, cfg.yolo.conf, cfg.yolo.threads)
        print(("✓ " if yolo else "! ") + msg)
    cam = CameraStream(cfg.camera.host, cfg.camera.port, yolo=yolo)
    print(f"Жду кадры с {cam.address} (сим запущен с --enable_cameras?) …")
    try:
        while not cam.alive():
            time.sleep(0.2)
        print("✓ Кадры идут." + ("" if save else " q — выход."))
        last = 0.0
        while True:
            frame, det, objs = cam.snapshot()
            if frame is None:
                time.sleep(0.05)
                continue
            img = annotate(frame, det, objs if cam.yolo else [])
            if save:
                if time.monotonic() - last > 1.0:
                    os.makedirs(save, exist_ok=True)
                    path = os.path.join(save, f"frame_{int(time.time())}.jpg")
                    cv2.imwrite(path, img)
                    print(path, "—", "; ".join(o.describe() for o in cam.visible()) or "ничего не вижу")
                    last = time.monotonic()
                time.sleep(0.05)
            else:
                try:
                    cv2.imshow("G1: head camera", img)
                except cv2.error:
                    print("✗ У opencv-python-headless нет окон — используй --save папка или окно g1-studio")
                    return 1
                if cv2.waitKey(30) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
    return 0


def scene_cmd(cfg: Config, what: str, state: str | None) -> int:
    from g1_studio.sim import scene
    from g1_studio.sim.process import ManagedProcess, SimError, env_python_command

    repo = cfg.sim_repo
    try:
        if what == "status":
            for line in scene.status(repo).describe():
                print(line)
            return 0
        if what == "find":
            out = repo / scene.OBJECTS_JSON
            cmd = env_python_command(cfg, str(scene.find_objects_script()), "--out", str(out))
            proc = ManagedProcess("поиск предметов", on_line=print)
            proc.start(cmd, repo)
            while proc.running:
                time.sleep(0.2)
            time.sleep(0.3)
            return proc.returncode or 0
        if state is None:
            print("Укажи on или off: g1-studio scene", what, "on")
            return 2
        fn = scene.set_ball if what == "ball" else scene.set_objects
        changed = fn(repo, state == "on")
        name = "Мяч" if what == "ball" else "Предметы"
        print(f"✓ {name}: {'включено' if state == 'on' else 'убрано'}" if changed else f"{name}: уже так")
        if changed:
            print("  Перезапусти сим.")
        return 0
    except (scene.SceneError, SimError) as e:
        print(f"✗ {e}")
        return 1


def sim_cmd(cfg: Config, only_print: bool) -> int:
    import os
    import subprocess

    from g1_studio.sim.process import SimError, sim_command

    try:
        cmd, cwd, env = sim_command(cfg)
    except SimError as e:
        print(f"✗ {e}")
        return 1
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items() if k != "PYTHONUNBUFFERED")
    print(f"cd {shlex.quote(str(cwd))} && {envs} {shlex.join(cmd)}")
    if only_print:
        return 0
    try:
        return subprocess.call(cmd, cwd=cwd, env={**os.environ, **env})
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    logging.basicConfig()
    sys.exit(main())
