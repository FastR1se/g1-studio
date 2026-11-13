"""Мост между фоновыми потоками и интерфейсом + мелкие общие виджеты."""
from __future__ import annotations

import html
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QLabel

GREEN, YELLOW, RED, GREY = "#2e7d32", "#c77700", "#c62828", "#808080"


class Bridge(QObject):
    """Сигналы Qt можно испускать из любого потока — обработчики выполнятся в потоке интерфейса."""
    event = Signal(str, object)            # (вид события, данные) — от Studio/Controller
    log_record = Signal(str, int, str)     # (источник, уровень, текст)
    command_done = Signal(object)          # CommandResult


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: Bridge, source: str = "app"):
        super().__init__(logging.INFO)
        self.bridge = bridge
        self.source = source
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.log_record.emit(self.source, record.levelno, self.format(record))
        except RuntimeError:          # окно уже закрыто
            pass


class Dot(QLabel):
    """Цветной кружок-индикатор + текст."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.set(GREY, text)

    def set(self, color: str, text: str, tooltip: str = "") -> None:
        self.setText(f'<span style="color:{color}">●</span> {html.escape(text)}')
        self.setToolTip(tooltip or text)


def esc(text: str) -> str:
    return html.escape(text)
