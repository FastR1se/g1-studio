"""Окно «Настройки»: формы строятся сами по полям Config (подписи и подсказки — из config.py)."""
from __future__ import annotations

from dataclasses import fields

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from g1_studio.config import SECTIONS, Config

RESTART_NOTE = {"sim": " (после перезапуска сима)", "app": " (после перезапуска приложения)"}


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки — G1 Studio")
        self.resize(640, 480)
        self.cfg = cfg
        self.editors: dict[tuple[str, str], tuple[QWidget, object]] = {}

        self.tabs = QTabWidget()
        for name, (title, _cls) in SECTIONS.items():
            page = QWidget()
            form = QFormLayout(page)
            section = getattr(cfg, name)
            for f in fields(section):
                w, getter = self._editor(f, getattr(section, f.name))
                label = QLabel(f.metadata["label"] + ":")
                tip = f.metadata["help"] + RESTART_NOTE.get(f.metadata["restart"], "")
                if tip:
                    label.setToolTip(tip)
                    w.setToolTip(tip)
                form.addRow(label, w)
                self.editors[(name, f.name)] = (w, getter)
            self.tabs.addTab(page, title)

        path = QLabel(f"Файл: {cfg.path}")
        path.setWordWrap(True)
        open_btn = QPushButton("Открыть файл")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(cfg.path))))
        defaults = QPushButton("По умолчанию (вкладка)")
        defaults.clicked.connect(self.reset_tab)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(path, 1)
        bottom.addWidget(open_btn)
        bottom.addWidget(defaults)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)
        lay.addWidget(buttons)

    # ------------------------------------------------------------------
    def _editor(self, f, value):
        m = f.metadata
        if m["choices"]:
            w = QComboBox()
            w.addItems(m["choices"])
            w.setCurrentText(str(value))
            return w, w.currentText
        if isinstance(f.default, bool):
            w = QCheckBox()
            w.setChecked(value)
            return w, w.isChecked
        if isinstance(f.default, int):
            w = QSpinBox()
            w.setRange(int(m["min"] if m["min"] is not None else -10**9), int(m["max"] if m["max"] is not None else 10**9))
            w.setValue(value)
            return w, w.value
        if isinstance(f.default, float):
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setRange(m["min"] if m["min"] is not None else -1e9, m["max"] if m["max"] is not None else 1e9)
            w.setSingleStep(0.05 if (m["max"] or 10) <= 2 else 1.0)
            w.setValue(value)
            return w, w.value
        line = QLineEdit(str(value))
        if m["kind"] == "secret":
            line.setEchoMode(QLineEdit.Password)
        if m["kind"] in ("dir", "file"):
            box = QWidget()
            hl = QHBoxLayout(box)
            hl.setContentsMargins(0, 0, 0, 0)
            btn = QPushButton("Обзор…")
            hl.addWidget(line, 1)
            hl.addWidget(btn)

            def browse():
                if m["kind"] == "dir":
                    p = QFileDialog.getExistingDirectory(self, m["label"], line.text())
                else:
                    p, _ = QFileDialog.getOpenFileName(self, m["label"], line.text())
                if p:
                    line.setText(p)
            btn.clicked.connect(browse)
            return box, line.text
        return line, line.text

    def _set(self, w, value) -> None:
        if isinstance(w, QComboBox):
            w.setCurrentText(str(value))
        elif isinstance(w, QCheckBox):
            w.setChecked(value)
        elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.setValue(value)
        elif isinstance(w, QLineEdit):
            w.setText(str(value))
        else:
            w.findChild(QLineEdit).setText(str(value))

    def reset_tab(self) -> None:
        name = list(SECTIONS)[self.tabs.currentIndex()]
        default = SECTIONS[name][1]()
        for f in fields(default):
            self._set(self.editors[(name, f.name)][0], getattr(default, f.name))

    def result_config(self) -> Config:
        new = self.cfg.copy()
        for (name, key), (_w, getter) in self.editors.items():
            v = getter()
            if isinstance(v, str):
                v = v.strip()
            setattr(getattr(new, name), key, v)
        return new
