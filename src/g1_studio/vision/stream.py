"""Поток кадров с головной камеры сима.

sim_main.py (через teleimager) публикует головную камеру как JPEG по ZMQ PUB на
tcp://127.0.0.1:55555. Мяч ищется на каждом кадре, YOLO — в своём потоке на самом
свежем кадре (если не успевает за камерой, старые кадры пропускает).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from g1_studio.vision.detect import BallDetector, Detection, ObjectDetection, YoloDetector

log = logging.getLogger("g1studio.vision")

MODEL_FILES = ("yolo26s.onnx", "yolo26n.onnx")   # s точнее (людей издалека 27/30 против 24/30), n вдвое быстрее


def model_dirs() -> list[Path]:
    """Где искать модели: $G1_STUDIO_MODELS, models/ в репозитории, ~/.local/share/g1-studio/models."""
    dirs = []
    if os.environ.get("G1_STUDIO_MODELS"):
        dirs.append(Path(os.environ["G1_STUDIO_MODELS"]).expanduser())
    dirs.append(Path(__file__).resolve().parents[3] / "models")          # src/g1_studio/vision -> репо
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    dirs.append(base / "g1-studio" / "models")
    return dirs


def find_model(path: str = "") -> Path | None:
    """Путь к модели: явно заданный или первый найденный yolo26s/yolo26n."""
    if path:
        p = Path(path).expanduser()
        if p.is_file():
            return p
        for d in model_dirs():                     # имя без папки — ищем в папках моделей
            if (d / path).is_file():
                return d / path
        return None
    for name in MODEL_FILES:
        for d in model_dirs():
            if (d / name).is_file():
                return d / name
    return None


def load_yolo(path: str = "", conf: float = 0.35, threads: int | None = None) -> tuple[YoloDetector | None, str]:
    """(YOLO или None, сообщение для пользователя)."""
    model = find_model(path)
    if model is None:
        where = path or " / ".join(MODEL_FILES)
        return None, f"YOLO выключен: нет модели {where} (папки: {', '.join(str(d) for d in model_dirs())})"
    try:
        y = YoloDetector(str(model), conf, threads)
    except ImportError:
        return None, "YOLO выключен: нет onnxruntime (pip install onnxruntime)"
    except Exception as e:
        return None, f"YOLO не загрузился ({model.name}): {e}"
    return y, f"YOLO: {y.model_name}, {len(y.names)} классов"


class CameraStream:
    def __init__(self, host: str = "127.0.0.1", port: int = 55555,
                 detector: BallDetector | None = None, yolo: YoloDetector | None = None):
        import zmq
        self._zmq = zmq
        self.address = f"tcp://{host}:{port}"
        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.setsockopt(zmq.CONFLATE, 1)          # держим только самый свежий кадр
        self.sock.setsockopt(zmq.RCVTIMEO, 200)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.connect(self.address)
        self.detector = detector or BallDetector()
        self.yolo = yolo
        self._lock = threading.Lock()
        self._new_frame = threading.Event()
        self.frame: np.ndarray | None = None
        self.frame_t = 0.0
        self.det: Detection | None = None
        self._objects: list[ObjectDetection] = []
        self.objects_t = 0.0
        self.yolo_ms = 0.0
        self.frames = 0
        self._fps = 0.0
        self._fps_t0, self._fps_n0 = time.monotonic(), 0
        self._yolo_err_t = 0.0
        self._alive = True
        self._threads = [threading.Thread(target=self._loop, name="camera", daemon=True),
                         threading.Thread(target=self._yolo_loop, name="yolo", daemon=True)]
        for th in self._threads:
            th.start()

    # --- потоки ---
    def _loop(self) -> None:
        while self._alive:
            try:
                data = self.sock.recv()
            except self._zmq.Again:
                continue
            except self._zmq.ZMQError:
                break
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            t = time.monotonic()
            try:
                det = self.detector.detect(img, t)
            except Exception as e:
                log.error("детектор мяча: %s", e)
                det = None
            with self._lock:
                self.frame, self.frame_t, self.det = img, t, det
                self.frames += 1
            self._new_frame.set()

    def _yolo_loop(self) -> None:
        while self._alive:
            if not self._new_frame.wait(0.5):
                continue
            self._new_frame.clear()
            yolo = self.yolo
            if yolo is None:
                with self._lock:
                    self._objects = []
                continue
            with self._lock:
                frame, t = self.frame, self.frame_t
            if frame is None:
                continue
            t0 = time.perf_counter()
            try:
                objs = yolo.detect(frame, t)
            except Exception as e:                   # не роняем поток из-за одного кадра
                if time.monotonic() - self._yolo_err_t > 5:
                    log.error("YOLO: %s", e)
                    self._yolo_err_t = time.monotonic()
                continue
            ms = (time.perf_counter() - t0) * 1000
            with self._lock:
                if self.yolo is yolo:                 # модель не поменяли, пока считали
                    self._objects, self.objects_t = objs, t
                    self.yolo_ms = 0.8 * self.yolo_ms + 0.2 * ms if self.yolo_ms else ms

    # --- настройки на ходу ---
    def set_yolo(self, yolo: YoloDetector | None) -> None:
        with self._lock:
            self.yolo = yolo
            self._objects, self.objects_t, self.yolo_ms = [], 0.0, 0.0

    # --- доступ ---
    def alive(self, max_age: float = 1.0) -> bool:
        return time.monotonic() - self.frame_t < max_age

    def fps(self) -> float:
        now = time.monotonic()
        if now - self._fps_t0 >= 1.0:
            self._fps = (self.frames - self._fps_n0) / (now - self._fps_t0)
            self._fps_t0, self._fps_n0 = now, self.frames
        return self._fps if self.alive() else 0.0

    def latest(self, max_age: float = 0.5) -> Detection | None:
        """Мяч на последнем кадре, если кадр свежий."""
        with self._lock:
            if self.det is None or time.monotonic() - self.frame_t > max_age:
                return None
            return self.det

    def objects(self, max_age: float = 0.8) -> list[ObjectDetection]:
        """Объекты YOLO с последнего обработанного кадра, если он свежий."""
        with self._lock:
            if time.monotonic() - self.objects_t > max_age:
                return []
            return list(self._objects)

    def find(self, target: str, max_age: float = 0.8):
        """Лучшая детекция цели: 'ball' — мяч по цвету, иначе класс YOLO (самый уверенный, при равенстве — ближний)."""
        if target == "ball":
            return self.latest(min(max_age, 0.5))
        cands = [o for o in self.objects(max_age) if o.label == target]
        if not cands:
            return None
        return max(cands, key=lambda o: (round(o.conf, 1), -o.distance_m))

    def visible(self) -> list:
        """Всё, что видно сейчас: мяч + объекты YOLO, ближние первыми."""
        items = list(self.objects())
        ball = self.latest()
        if ball is not None:
            items = [o for o in items if o.label != "sports ball"] + [ball]
        return sorted(items, key=lambda o: o.distance_m)

    def snapshot(self):
        """(кадр или None, мяч, объекты) — кадр без копии: не менять его на месте."""
        with self._lock:
            return self.frame, self.det, list(self._objects)

    def wait_frame(self, after_t: float, timeout: float = 1.0, yolo: bool = False) -> bool:
        """Дождаться кадра (или результата YOLO, если yolo=True), пришедшего после after_t."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (self.objects_t if yolo and self.yolo else self.frame_t) > after_t:
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        """Остановить потоки и дождаться их: иначе onnxruntime может быть прерван посреди кадра
        при выходе из Python («terminate called without an active exception»)."""
        self._alive = False
        self._new_frame.set()
        for th in self._threads:
            th.join(timeout=3)
        self.sock.close(0)
