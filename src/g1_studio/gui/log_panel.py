"""Логи приложения и вывод сима в одном окне, с фильтрами."""
from __future__ import annotations

import logging
from collections import deque

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from g1_studio import logs


class LogPanel(QWidget):
    MAX = 5000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: deque[tuple[str, int, str]] = deque(maxlen=self.MAX)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.MAX)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        self.view.setFont(mono)
        self.source = QComboBox()
        self.source.addItems(["Всё", "Приложение", "Сим"])
        self.level = QComboBox()
        self.level.addItems(["Всё", "Предупреждения и ошибки"])
        clear = QPushButton("Очистить")
        folder = QPushButton("Папка логов")
        folder.setToolTip(str(logs.log_dir()))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Показывать:"))
        bar.addWidget(self.source)
        bar.addWidget(self.level)
        bar.addStretch(1)
        bar.addWidget(clear)
        bar.addWidget(folder)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)

        self.source.currentIndexChanged.connect(self.refilter)
        self.level.currentIndexChanged.connect(self.refilter)
        clear.clicked.connect(self.clear)
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs.log_dir()))))

    def _visible(self, source: str, level: int) -> bool:
        s = self.source.currentIndex()
        if (s == 1 and source != "app") or (s == 2 and source == "app"):
            return False
        return not (self.level.currentIndex() == 1 and level < logging.WARNING)

    @staticmethod
    def _fmt(source: str, text: str) -> str:
        return text if source == "app" else f"[{source}] {text}"

    def add(self, source: str, level: int, text: str) -> None:
        self.records.append((source, level, text))
        if self._visible(source, level):
            bar = self.view.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            self.view.appendPlainText(self._fmt(source, text))
            if at_end:
                bar.setValue(bar.maximum())

    def refilter(self) -> None:
        self.view.setPlainText("\n".join(self._fmt(s, t) for s, lv, t in self.records if self._visible(s, lv)))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def clear(self) -> None:
        self.records.clear()
        self.view.clear()
