"""Состояние: сим, связь, курс, камера, YOLO, Qwen, что делает робот."""
from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QHBoxLayout, QPushButton, QWidget

from g1_studio.gui.common import GREEN, GREY, RED, YELLOW, Dot


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 60} мин {s % 60:02d} с" if s >= 60 else f"{s} с"


class StatusPanel(QGroupBox):
    apply_sim_speed = Signal(float)

    def __init__(self, parent=None):
        super().__init__("Состояние", parent)
        self.sim = Dot()
        self.dds = Dot()
        self.yaw = Dot()
        self.cam = Dot()
        self.yolo = Dot()
        self.qwen = Dot()
        self.robot = Dot()
        self.speed = Dot()
        self.speed_btn = QPushButton("Применить")
        self.speed_btn.setVisible(False)
        self._hint: float | None = None
        self.speed_btn.clicked.connect(lambda: self._hint and self.apply_sim_speed.emit(self._hint))
        speed_row = QWidget()
        sl = QHBoxLayout(speed_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.speed, 1)
        sl.addWidget(self.speed_btn)

        form = QFormLayout(self)
        form.addRow("Сим:", self.sim)
        form.addRow("Связь:", self.dds)
        form.addRow("Курс:", self.yaw)
        form.addRow("Камера:", self.cam)
        form.addRow("YOLO:", self.yolo)
        form.addRow("Qwen:", self.qwen)
        form.addRow("Робот:", self.robot)
        form.addRow("Скорость сима:", speed_row)

    def update_status(self, st: dict, cfg) -> None:
        connected = st["state_age"] < 1.0
        # --- сим ---
        if st["sim_running"]:
            if connected:
                self.sim.set(GREEN, f"работает (запущен отсюда, {_dur(st['sim_uptime'])})")
            else:
                self.sim.set(YELLOW, f"запускается… {_dur(st['sim_uptime'])} (Isaac Sim грузится 1–3 мин)")
        elif st["sim_external"]:
            self.sim.set(GREEN if connected else YELLOW, f"запущен вне приложения (pid {st['sim_external']})")
        elif connected:
            self.sim.set(GREEN, "работает (запущен не отсюда)")
        else:
            self.sim.set(GREY, "не запущен — «▶ Запустить сим» или запусти sim_main.py сам")
        # --- связь ---
        if st["link_note"]:
            self.dds.set(RED if "dry" not in st["link_kind"] else GREY, st["link_note"])
        elif connected:
            self.dds.set(GREEN, f"{st['link_kind']}, rt/lowstate {st['state_rate']:.0f} Гц")
        else:
            self.dds.set(GREY, f"{st['link_kind']}: жду rt/lowstate")
        self.yaw.set(GREEN if st["yaw_deg"] is not None else GREY,
                     f"{st['yaw_deg']:+.0f}°" if st["yaw_deg"] is not None else "—")
        # --- камера ---
        if not st["camera"]:
            self.cam.set(GREY, st["camera_note"] or "выключена")
        elif st["camera_alive"]:
            self.cam.set(GREEN, f"{st['camera_fps']:.0f} к/с ({st['camera_addr']})")
        else:
            self.cam.set(GREY, f"жду кадры ({st['camera_addr']})")
        if not st["camera"]:
            self.yolo.set(GREY, "нужна камера")
        elif st["yolo"]:
            ms = f", {st['yolo_ms']:.0f} мс" if st["yolo_ms"] else ""
            self.yolo.set(GREEN, f"{st['yolo_model']}{ms}")
        else:
            off = not cfg.yolo.enabled
            self.yolo.set(GREY if off else RED, "выключен" if off else st["yolo_note"] or "не загрузился")
        # --- qwen ---
        ok = st["qwen_ok"]
        self.qwen.set(GREY if ok is None else GREEN if ok else RED, st["qwen_text"])
        # --- робот ---
        vx, vy, wz, h = st["cmd"]
        moving = abs(vx) > 0.01 or abs(vy) > 0.01 or abs(wz) > 0.02
        what = {"plan": st["step"] or "выполняю план", "manual": "ручное управление", "idle": "стоит"}[st["mode"]]
        if moving or st["mode"] != "idle":
            what += f" · vx {vx:+.2f} vy {vy:+.2f} м/с, wz {math.degrees(wz):+.0f}°/с"
        if h < 0.79:
            what += f" · высота {h:.2f} м"
        self.robot.set(GREEN if st["mode"] != "idle" else GREY, what)
        # --- скорость сима ---
        est = st["sim_speed_est"]
        txt = f"{cfg.robot.sim_speed:.2f}× (в настройках)"
        self._hint = None
        if est is not None:
            txt += f", по поворотам ~{est:.2f}×"
            if est < 0.75 and abs(est - cfg.robot.sim_speed) > 0.15:
                self._hint = round(est, 2)
        self.speed.set(YELLOW if self._hint else GREY, txt)
        self.speed_btn.setVisible(self._hint is not None)
