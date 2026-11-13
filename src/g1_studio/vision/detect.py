"""Детекторы: красный мяч по цвету и объекты COCO через YOLO (ONNX, onnxruntime на CPU)."""
from __future__ import annotations

import ast
import math
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

from g1_studio.names import COCO_NAMES, name_ru
from g1_studio.vision.geometry import IMG_H, IMG_W, ground_locate, locate


def _side(bearing_deg: float) -> str:
    if abs(bearing_deg) < 5:
        return "прямо"
    return f"{abs(bearing_deg):.0f}° {'слева' if bearing_deg > 0 else 'справа'}"


@dataclass
class Detection:
    """Красный мяч (детектор по цвету)."""
    u: float                 # центр на картинке, px
    v: float
    r_px: float              # видимый радиус (поперёк луча), px
    bearing_deg: float       # направление: + налево, − направо
    distance_m: float        # горизонталь от торса до центра мяча
    height_m: float          # оценка высоты центра мяча над полом (проверка правдоподобия)
    clipped: bool            # мяч обрезан краем кадра
    score: float
    t: float                 # время кадра (monotonic)
    label: str = "ball"

    @property
    def name_ru(self) -> str:
        return "мяч"

    def describe(self) -> str:
        return f"мяч {_side(self.bearing_deg)}, {self.distance_m:.2f} м"


@dataclass
class ObjectDetection:
    """Объект, найденный YOLO."""
    label: str               # класс COCO (англ.)
    conf: float
    box: tuple[float, float, float, float]   # x1, y1, x2, y2, px
    bearing_deg: float
    distance_m: float        # горизонталь от торса до точки, где объект стоит на полу
    clipped: bool            # низ объекта за кадром — расстояние завышено
    t: float

    @property
    def name_ru(self) -> str:
        return name_ru(self.label)

    def describe(self) -> str:
        far = " (низ за кадром — ближе, чем кажется)" if self.clipped else ""
        return f"{self.name_ru} {_side(self.bearing_deg)}, {self.distance_m:.2f} м{far}"


# ----------------------------------------------------------------------------
# Детектор красного мяча
# ----------------------------------------------------------------------------
class BallDetector:
    # Край пятна «съедается» порогом цвета и JPEG: видимый радиус меньше настоящего ~на 1 px.
    # Вблизи (16 px) это мелочь, вдали (8 px) — 12% ошибки дистанции, поэтому поправляем.
    EDGE_BIAS_PX = 1.0

    def __init__(self, min_area: int = 40):
        self.min_area = min_area
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.kernel_split = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))

    def mask(self, bgr: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, (0, 120, 60), (10, 255, 255)) | cv2.inRange(hsv, (170, 120, 60), (180, 255, 255))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.kernel)
        return cv2.morphologyEx(m, cv2.MORPH_CLOSE, self.kernel)

    def detect(self, bgr: np.ndarray, t: float | None = None) -> Detection | None:
        h, w = bgr.shape[:2]
        if (w, h) != (IMG_W, IMG_H):
            bgr = cv2.resize(bgr, (IMG_W, IMG_H))
        m = self.mask(bgr)
        best = self._best(m, t)
        if best is None:
            # Второй проход: мяч мог слиться с другим красным предметом (полоса, край ящика).
            # Более сильное «открытие» отрезает тонкие перемычки, круглое пятно остаётся.
            split = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.kernel_split)
            best = self._best(split, t)
        return best

    def _best(self, m: np.ndarray, t: float | None) -> Detection | None:
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        best = None
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            clipped = x <= 1 or y <= 1 or x + bw >= IMG_W - 1 or y + bh >= IMG_H - 1
            (ex, ey), enc_r = cv2.minEnclosingCircle(c)
            fill = area / (math.pi * enc_r * enc_r + 1e-6)        # круг ≈ 0.8–1.0, квадрат ≈ 0.64, полоса — мало
            # Мяч, обрезанный краем кадра, заполняет круг хуже (половинка ≈ 0.5, четвертинка ≈ 0.3),
            # но красная полоса/кромка у края — ещё хуже (≈ 0.05). Без этого порога полоса у края = «мяч».
            if fill < (0.3 if clipped else 0.6):
                continue
            if len(c) >= 5 and not clipped:
                (u, v), (a1, a2), _ = cv2.fitEllipse(c)
                r_px = min(a1, a2) / 2.0 + self.EDGE_BIAS_PX      # поперечная полуось — надёжнее площади
            else:
                mo = cv2.moments(c)
                u, v = (mo["m10"] / mo["m00"], mo["m01"] / mo["m00"]) if mo["m00"] else (ex, ey)
                if clipped:
                    u, v = ex, ey
                r_px = enc_r
            bearing, dist, height = locate(u, v, r_px, clipped)
            if not math.isfinite(dist) or not (-0.25 < height < 0.6):
                continue                                          # «мяч» в воздухе/на стене — не наш
            score = area * fill
            if best is None or score > best.score:
                best = Detection(u, v, r_px, bearing, dist, height, clipped, score,
                                 t if t is not None else time.monotonic())
        return best


# ----------------------------------------------------------------------------
# YOLO (ONNX)
# ----------------------------------------------------------------------------
class YoloDetector:
    """YOLO в формате ONNX через onnxruntime.

    Понимает оба вида экспорта Ultralytics:
      * end-to-end (YOLO26, export(..., end2end=True)): выход (1, N, 6) = x1, y1, x2, y2, score, класс — без NMS;
      * классический (YOLOv8/11, YOLO26 без end2end): выход (1, 4+классы, N) — NMS делаем сами.
    """

    def __init__(self, model_path: str, conf: float = 0.35, threads: int | None = None):
        import onnxruntime as ort
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"нет файла модели {model_path}")
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or min(4, os.cpu_count() or 4)   # остальное ядро — симу
        self.sess = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        self.size = inp.shape[2] if isinstance(inp.shape[2], int) else 640
        meta = self.sess.get_modelmeta().custom_metadata_map
        try:
            names = ast.literal_eval(meta["names"])
            self.names = [names[i] for i in range(len(names))] if isinstance(names, dict) else list(names)
        except (KeyError, ValueError, SyntaxError):
            self.names = COCO_NAMES
        self.conf = conf
        self.model_name = os.path.basename(model_path)

    def _letterbox(self, bgr: np.ndarray):
        h, w = bgr.shape[:2]
        r = min(self.size / h, self.size / w)
        nh, nw = round(h * r), round(w * r)
        top, left = (self.size - nh) // 2, (self.size - nw) // 2
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return x, r, left, top

    def detect(self, bgr: np.ndarray, t: float | None = None) -> list[ObjectDetection]:
        h, w = bgr.shape[:2]
        if (w, h) != (IMG_W, IMG_H):
            bgr = cv2.resize(bgr, (IMG_W, IMG_H))
        x, r, left, top = self._letterbox(bgr)
        out = self.sess.run(None, {self.input_name: x})[0]
        rows = self._decode(out)
        t = t if t is not None else time.monotonic()
        dets = []
        for x1, y1, x2, y2, score, cls in rows:
            x1, x2 = [min(max((v - left) / r, 0.0), IMG_W - 1.0) for v in (x1, x2)]
            y1, y2 = [min(max((v - top) / r, 0.0), IMG_H - 1.0) for v in (y1, y2)]
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            label = self.names[int(cls)] if int(cls) < len(self.names) else str(int(cls))
            bearing, dist = ground_locate((x1 + x2) / 2, y2)       # точка, где объект стоит на полу
            if not math.isfinite(dist):
                continue
            dets.append(ObjectDetection(label, float(score), (x1, y1, x2, y2), bearing, dist,
                                        y2 >= IMG_H - 3, t))
        return dets

    def _decode(self, out: np.ndarray):
        out = out[0]
        if out.ndim == 2 and out.shape[1] == 6:                    # end-to-end: уже без дублей
            return [row for row in out if row[4] >= self.conf]
        n = 4 + len(getattr(self, "names", COCO_NAMES))
        if out.shape[0] == n:
            pred = out.T                                           # (4+классы, N) -> (N, 4+классы)
        elif out.shape[1] == n:
            pred = out
        else:
            pred = out.T if out.shape[0] < out.shape[1] else out
        scores = pred[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = conf >= self.conf
        if not keep.any():
            return []
        b, conf, cls = pred[keep, :4], conf[keep], cls[keep]
        xywh = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 2], b[:, 3]], 1)
        idx = cv2.dnn.NMSBoxesBatched(xywh.tolist(), conf.tolist(), cls.tolist(), self.conf, 0.5)
        return [(xywh[i, 0], xywh[i, 1], xywh[i, 0] + xywh[i, 2], xywh[i, 1] + xywh[i, 3], conf[i], cls[i])
                for i in np.array(idx).flatten()]


# ----------------------------------------------------------------------------
# Разметка
# ----------------------------------------------------------------------------
def annotate(bgr: np.ndarray, det: Detection | None, objects: list[ObjectDetection] | None = None) -> np.ndarray:
    img = bgr.copy()
    for o in objects or []:
        x1, y1, x2, y2 = (int(v) for v in o.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 160, 0), 2)
        cv2.circle(img, ((x1 + x2) // 2, y2), 4, (255, 160, 0), -1)          # точка на полу
        txt = f"{o.label} {o.conf:.2f} | {o.bearing_deg:+.0f} deg {o.distance_m:.1f} m"
        cv2.putText(img, txt, (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 160, 0), 2)
    if det is not None:
        cv2.circle(img, (int(det.u), int(det.v)), max(3, int(det.r_px)), (0, 255, 0), 2)
        cv2.drawMarker(img, (int(det.u), int(det.v)), (0, 255, 0), cv2.MARKER_CROSS, 12, 2)
        txt = f"ball {det.bearing_deg:+.0f} deg {det.distance_m:.2f} m{' (edge)' if det.clipped else ''}"
        cv2.putText(img, txt, (10, IMG_H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    return img
