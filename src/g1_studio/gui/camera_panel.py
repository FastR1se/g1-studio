"""Камера: живой кадр с разметкой (мяч, рамки YOLO), список того, что видно, настройки YOLO."""
from __future__ import annotations

import time
from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from g1_studio.vision.detect import annotate


class ImageView(QLabel):
    """Картинка, вписанная в виджет с сохранением пропорций."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#202020; color:#bbbbbb;")
        self._pix: QPixmap | None = None

    def set_image(self, pix: QPixmap | None, placeholder: str = "") -> None:
        self._pix = pix
        if pix is None:
            self.setPixmap(QPixmap())
            self.setText(placeholder)
        else:
            self.setText("")
            self._refit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit()

    def _refit(self):
        if self._pix is not None:
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class CameraPanel(QWidget):
    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self._last_frame_id = None
        self._last_img = None

        self.view = ImageView()
        self.yolo_box = QCheckBox("YOLO")
        self.yolo_box.setToolTip("Распознавание предметов нейросетью (80 классов COCO). Мяч ищется всегда, по цвету.")
        self.conf = QDoubleSpinBox()
        self.conf.setRange(0.05, 0.95)
        self.conf.setSingleStep(0.05)
        self.conf.setDecimals(2)
        self.conf.setPrefix("порог ")
        self.conf.setToolTip("Порог уверенности YOLO: выше — меньше ложных, но и находит реже")
        self.info = QLabel("")
        self.save_btn = QPushButton("Сохранить кадр")
        self.seen = QListWidget()
        self.seen.setMaximumHeight(130)
        self.seen.setToolTip("Что видно сейчас: направление (+ слева) и расстояние по полу от робота")

        bar = QHBoxLayout()
        bar.addWidget(self.yolo_box)
        bar.addWidget(self.conf)
        bar.addWidget(self.info, 1)
        bar.addWidget(self.save_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)
        lay.addWidget(QLabel("Что видно:"))
        lay.addWidget(self.seen)

        self.sync_from_config()
        self.yolo_box.toggled.connect(lambda on: self.studio.set_yolo(enabled=on))
        self.conf.editingFinished.connect(lambda: self.studio.set_yolo(conf=self.conf.value()))
        self.save_btn.clicked.connect(self.save_frame)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.set_fps(studio.cfg.ui.camera_fps)

    def sync_from_config(self) -> None:
        for w in (self.yolo_box, self.conf):
            w.blockSignals(True)
        self.yolo_box.setChecked(self.studio.cfg.yolo.enabled)
        self.conf.setValue(self.studio.cfg.yolo.conf)
        for w in (self.yolo_box, self.conf):
            w.blockSignals(False)

    def set_fps(self, fps: int) -> None:
        self.timer.start(max(15, int(1000 / max(fps, 1))))

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        cam = self.studio.camera
        if cam is None:
            self.view.set_image(None, self.studio.camera_note or "Камера выключена")
            self.info.setText("")
            self.seen.clear()
            return
        frame, det, objs = cam.snapshot()
        if frame is None or not cam.alive(2.0):
            wait = "Жду кадры с камеры\n" + cam.address
            if frame is not None:
                wait = "Кадры перестали приходить\n" + cam.address
            self.view.set_image(None, wait + "\n\nСим запущен с --enable_cameras?")
            self.info.setText("")
            self.seen.clear()
            return
        if not cam.yolo:
            objs = []
        frame_id = (cam.frames, cam.objects_t)
        if frame_id != self._last_frame_id:
            self._last_frame_id = frame_id
            img = annotate(frame, det, objs)
            self._last_img = img
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
            self._overlay(qimg)
            self.view.set_image(QPixmap.fromImage(qimg))
            items = cam.visible()
            self.seen.clear()
            self.seen.addItems([o.describe() for o in items] or ["ничего знакомого"])
        txt = f"{cam.fps():.0f} к/с"
        if cam.yolo:
            txt += f" · {cam.yolo.model_name} {cam.yolo_ms:.0f} мс"
        elif self.studio.cfg.yolo.enabled:
            txt += " · YOLO не загрузился"
        self.info.setText(txt)

    def _overlay(self, qimg: QImage) -> None:
        """Курс робота в углу кадра."""
        st = self.studio.link
        yaw = st.yaw() if st else None
        if yaw is None:
            return
        p = QPainter(qimg)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPointSize(11)
        p.setFont(f)
        p.drawText(10, 22, f"курс {yaw * 57.29578:+.0f}°")
        p.end()

    def save_frame(self) -> None:
        if self._last_img is None:
            return
        default = str(Path.home() / f"g1_frame_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить кадр", default, "Картинки (*.jpg *.png)")
        if path:
            cv2.imwrite(path, self._last_img)
