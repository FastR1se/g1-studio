"""Ручное управление: кнопки и клавиатура (WASD — идти, Q/E — поворот, пробел — стоп).

Робот едет, пока кнопка/клавиша зажата. Отпустил — плавно встал. Окно потеряло фокус —
все «зажатые» клавиши отпускаются (чтобы робот не уехал, пока ты в другом окне).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSlider, QTextEdit, QVBoxLayout, QWidget,
)

# направление: (vx, vy, wz) в долях от скорости
MOVES = {
    "fwd": (1, 0, 0), "back": (-1, 0, 0), "left": (0, 1, 0), "right": (0, -1, 0),
    "turn_l": (0, 0, 1), "turn_r": (0, 0, -1),
}
# латиница и та же клавиша в русской раскладке
KEYS = {
    "w": "fwd", "ц": "fwd", "s": "back", "ы": "back", "a": "left", "ф": "left", "d": "right", "в": "right",
    "q": "turn_l", "й": "turn_l", "e": "turn_r", "у": "turn_r",
}
QT_KEYS = {Qt.Key_W: "fwd", Qt.Key_S: "back", Qt.Key_A: "left", Qt.Key_D: "right", Qt.Key_Q: "turn_l",
           Qt.Key_E: "turn_r", Qt.Key_Up: "fwd", Qt.Key_Down: "back", Qt.Key_Left: "turn_l", Qt.Key_Right: "turn_r"}
TEXT_INPUTS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)


class HoldButton(QPushButton):
    held = Signal(str, bool)

    def __init__(self, text: str, move: str):
        super().__init__(text)
        self.move = move
        self.setMinimumSize(64, 48)
        self.setFocusPolicy(Qt.NoFocus)
        self.pressed.connect(lambda: self.held.emit(self.move, True))
        self.released.connect(lambda: self.held.emit(self.move, False))


class DrivePanel(QWidget):
    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.pressed: set[str] = set()

        grid = QGridLayout()
        self.buttons = {}
        for (r, c), (label, move) in {
            (0, 0): ("↺ Q", "turn_l"), (0, 1): ("↑ W", "fwd"), (0, 2): ("↻ E", "turn_r"),
            (1, 0): ("← A", "left"), (1, 2): ("→ D", "right"), (2, 1): ("↓ S", "back"),
        }.items():
            b = HoldButton(label, move)
            b.held.connect(self.set_held)
            grid.addWidget(b, r, c)
            self.buttons[move] = b
        self.stop_btn = QPushButton("■ Стоп")
        self.stop_btn.setMinimumSize(64, 48)
        self.stop_btn.setFocusPolicy(Qt.NoFocus)
        self.stop_btn.clicked.connect(self.stop)
        grid.addWidget(self.stop_btn, 1, 1)

        self.keyboard = QCheckBox("Клавиатура: W/S — вперёд/назад, A/D — вбок, Q/E — поворот, пробел — стоп")
        self.keyboard.setChecked(studio.cfg.ui.keyboard)
        self.keyboard.setToolTip("Работает, когда курсор не в поле ввода. Русская раскладка тоже понимается.")

        speeds = QGroupBox("Скорость")
        form = QFormLayout(speeds)
        self.v = self._spin(0.1, 1.0, 0.05, studio.cfg.robot.manual_speed, " м/с")
        self.vy = self._spin(0.1, 0.5, 0.05, studio.cfg.robot.manual_strafe, " м/с")
        self.wz = self._spin(10, 90, 5, studio.cfg.robot.manual_turn, " °/с", 0)
        form.addRow("Вперёд/назад", self.v)
        form.addRow("Вбок", self.vy)
        form.addRow("Поворот", self.wz)

        height = QGroupBox("Высота корпуса")
        hl = QHBoxLayout(height)
        self.h = QSlider(Qt.Horizontal)
        self.h.setRange(30, 80)
        self.h.setValue(80)
        self.h_label = QLabel("0.80 м")
        squat, stand = QPushButton("Присесть"), QPushButton("Встать")
        hl.addWidget(self.h, 1)
        hl.addWidget(self.h_label)
        hl.addWidget(squat)
        hl.addWidget(stand)
        self.h.valueChanged.connect(self._height)
        squat.clicked.connect(lambda: self.h.setValue(50))
        stand.clicked.connect(lambda: self.h.setValue(80))

        self.state = QLabel("")
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addLayout(grid)
        top.addWidget(speeds, 1)
        lay.addLayout(top)
        lay.addWidget(self.keyboard)
        lay.addWidget(height)
        lay.addWidget(self.state)
        lay.addStretch(1)
        for w in (self.v, self.vy, self.wz):
            w.valueChanged.connect(self._apply)

    @staticmethod
    def _spin(lo, hi, step, val, suffix, decimals=2):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(decimals)
        s.setValue(val)
        s.setSuffix(suffix)
        return s

    # ------------------------------------------------------------------
    def set_held(self, move: str, on: bool) -> None:
        before = set(self.pressed)
        (self.pressed.add if on else self.pressed.discard)(move)
        if self.pressed != before:
            self._apply()

    def release_all(self) -> None:
        if self.pressed:
            self.pressed.clear()
            self._apply()

    def _apply(self) -> None:
        vx = vy = wz = 0.0
        for m in self.pressed:
            dx, dy, dw = MOVES[m]
            vx += dx
            vy += dy
            wz += dw
        ctl = self.studio.controller
        if ctl is None:
            return
        if not self.pressed and ctl.mode != "manual":
            return                                         # не мешаем плану Qwen, если ручное не трогали
        ctl.manual(vx * self.v.value(), vy * self.vy.value(), wz * self.wz.value())
        names = {"fwd": "вперёд", "back": "назад", "left": "влево", "right": "вправо",
                 "turn_l": "поворот налево", "turn_r": "поворот направо"}
        self.state.setText("Еду: " + ", ".join(names[m] for m in sorted(self.pressed)) if self.pressed else "")
        for m, b in self.buttons.items():
            b.setDown(m in self.pressed)

    def _height(self, v: int) -> None:
        h = v / 100
        self.h_label.setText(f"{h:.2f} м")
        if self.studio.controller is not None:
            self.studio.controller.set_height(h)

    def stop(self) -> None:
        self.pressed.clear()
        for b in self.buttons.values():
            b.setDown(False)
        self.studio.stop()
        self.state.setText("")


class KeyboardFilter(QObject):
    """Перехват клавиш на уровне приложения: WASD/QE работают, пока фокус не в поле ввода."""

    def __init__(self, panel: DrivePanel, window: QWidget):
        super().__init__(window)
        self.panel = panel
        self.window = window

    def _move_for(self, e) -> str | None:
        if e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return None
        return KEYS.get(e.text().lower()) or QT_KEYS.get(e.key())

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t in (QEvent.ApplicationDeactivate, QEvent.WindowDeactivate):
            self.panel.release_all()
            return False
        if t not in (QEvent.KeyPress, QEvent.KeyRelease) or not self.panel.keyboard.isChecked():
            return False
        if not self.window.isActiveWindow():
            return False
        from PySide6.QtWidgets import QApplication
        focus = QApplication.focusWidget()
        if isinstance(focus, TEXT_INPUTS):
            return False
        if e.key() == Qt.Key_Space:
            if t == QEvent.KeyPress and not e.isAutoRepeat():
                self.panel.stop()
            return True
        move = self._move_for(e)
        if move is None:
            return False
        if not e.isAutoRepeat():
            self.panel.set_held(move, t == QEvent.KeyPress)
        return True
