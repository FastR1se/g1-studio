"""g1-studio doctor — проверка установки: что на месте, что нет и как починить."""
from __future__ import annotations

import ctypes.util
import importlib
import importlib.metadata as md
import platform
import shutil
import subprocess
import sys
import time

from g1_studio import APP_NAME, __version__

OK, WARN, FAIL = "✓", "!", "✗"


class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def line(self, mark: str, text: str, fix: str = "") -> None:
        if mark == FAIL:
            self.fails += 1
        elif mark == WARN:
            self.warns += 1
        print(f"  {mark} {text}")
        if fix:
            print(f"      → {fix}")


def _ver(dist: str) -> str | None:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def run(cfg) -> int:
    r = Report()
    print(f"{APP_NAME} {__version__} — проверка\n")

    print("Окружение приложения")
    py = sys.version_info
    r.line(OK if py >= (3, 10) else FAIL, f"Python {platform.python_version()} ({sys.executable})",
           "" if py >= (3, 10) else "нужен Python 3.10+")
    try:
        import PySide6
        r.line(OK, f"PySide6 {PySide6.__version__}")
    except ImportError:
        r.line(FAIL, "нет PySide6", "pip install -e .  (или bash scripts/setup_studio.sh)")
    if not ctypes.util.find_library("xcb-cursor"):
        r.line(WARN, "нет libxcb-cursor0 — окно Qt может не открыться под X11",
               "sudo apt install libxcb-cursor0")
    if _ver("opencv-python") and _ver("opencv-python-headless"):
        r.line(WARN, "стоят и opencv-python, и opencv-python-headless — Qt-плагины могут конфликтовать",
               "pip uninstall -y opencv-python && pip install --force-reinstall opencv-python-headless")
    for mod, dist in (("cv2", "opencv-python-headless"), ("zmq", "pyzmq"), ("onnxruntime", "onnxruntime"),
                      ("numpy", "numpy")):
        try:
            m = importlib.import_module(mod)
            r.line(OK, f"{dist} {getattr(m, '__version__', '')}")
        except Exception as e:
            r.line(FAIL, f"{dist}: {e}", f"pip install {dist}")

    from g1_studio.core.link import dds_available
    ok, why = dds_available()
    r.line(OK if ok else FAIL, f"unitree_sdk2py {_ver('unitree_sdk2py') or ''} + cyclonedds {_ver('cyclonedds') or ''}"
           if ok else why, "" if ok else "bash scripts/setup_studio.sh")

    from g1_studio.vision.stream import find_model, model_dirs
    m = find_model(cfg.yolo.model)
    r.line(OK if m else WARN, f"модель YOLO: {m}" if m else "модели YOLO нет",
           "" if m else f"положи yolo26s.onnx или yolo26n.onnx в {model_dirs()[1]}")

    print("\nСим")
    from g1_studio.sim.process import conda_envs, find_conda, find_external_sim
    conda = find_conda(cfg.sim.conda)
    r.line(OK if conda else FAIL, f"conda: {conda}" if conda else "conda не найдена",
           "" if conda else "укажи путь в настройках (sim.conda)")
    if conda:
        envs = conda_envs(conda)
        has = cfg.sim.conda_env in envs
        r.line(OK if has else FAIL, f"окружение {cfg.sim.conda_env}: {'есть' if has else 'нет'}",
               "" if has else "bash scripts/install_unitree_sim.sh")
    repo = cfg.sim_repo
    has_repo = (repo / "sim_main.py").is_file()
    r.line(OK if has_repo else FAIL, f"unitree_sim_isaaclab: {repo}",
           "" if has_repo else "bash scripts/install_unitree_sim.sh или поправь sim.repo в настройках")
    if has_repo:
        from g1_studio.sim import scene
        for line in scene.status(repo).describe():
            print(f"    {line}")
    if shutil.which("nvidia-smi"):
        try:
            drv = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if not drv or "," not in drv:
                raise RuntimeError("видеокарта не видна")
            ver = int(drv.split(",")[1].strip().split(".")[0])
            r.line(WARN if ver >= 590 else OK, f"GPU: {drv}",
                   "драйвер 59x роняет Isaac Sim 5.1 — bash scripts/switch_nvidia_580.sh" if ver >= 590 else "")
        except Exception as e:
            r.line(WARN, f"nvidia-smi: {e}")
    else:
        r.line(WARN, "nvidia-smi не найден — нет драйвера NVIDIA?")

    print("\nQwen")
    from g1_studio.core.planner import LLMError, QwenPlanner
    try:
        planner = QwenPlanner.from_config(cfg)
        planner.check()
        r.line(OK, f"{cfg.qwen.model} ({planner.where}) на {cfg.qwen.url}")
    except LLMError as e:
        fix = ""
        if cfg.qwen.backend == "ollama":
            fix = ("curl -fsSL https://ollama.com/install.sh | sh" if not shutil.which("ollama")
                   else f"ollama serve  и  ollama pull {cfg.qwen.model}")
        r.line(FAIL, str(e), fix)

    print("\nПрямо сейчас")
    ext = find_external_sim()
    print(f"    sim_main.py: {'запущен, pid ' + str(ext) if ext else 'не запущен'}")
    if ok:
        from g1_studio.core.link import SimLink
        link = SimLink(cfg.dds.domain, cfg.dds.interface or None, cfg.dds.quat_order)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 and link.yaw() is None:
            time.sleep(0.05)
        y = link.yaw()
        r.line(OK if y is not None else WARN,
               f"rt/lowstate идёт, курс {y * 57.3:+.0f}°" if y is not None else "rt/lowstate нет (сим не запущен?)")
    try:
        from g1_studio.vision.stream import CameraStream
        cam = CameraStream(cfg.camera.host, cfg.camera.port)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2 and not cam.alive():
            time.sleep(0.05)
        alive = cam.alive()
        cam.close()
        r.line(OK if alive else WARN, f"кадры с {cam.address}" + ("" if alive else ": нет (сим запущен с --enable_cameras?)"))
    except Exception as e:
        r.line(WARN, f"камера: {e}")

    print(f"\nИтого: ошибок {r.fails}, предупреждений {r.warns}. Настройки: {cfg.path}")
    return 1 if r.fails else 0
