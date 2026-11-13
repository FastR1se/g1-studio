"""Чат с Qwen: текст -> план -> пошаговое выполнение с отчётом робота."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from g1_studio.gui.common import GREEN, GREY, RED, YELLOW, esc

QUICK = ["Что ты видишь?", "Найди мяч", "Подойди к мячу", "Толкни мяч", "Повернись налево", "Пройди метр вперёд"]


class HistoryLine(QLineEdit):
    """Строка ввода с историей команд (стрелки вверх/вниз)."""
    def __init__(self):
        super().__init__()
        self.history: list[str] = []
        self.pos = 0

    def remember(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.pos = len(self.history)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Up and self.history:
            self.pos = max(0, self.pos - 1)
            self.setText(self.history[self.pos])
        elif e.key() == Qt.Key_Down and self.history:
            self.pos = min(len(self.history), self.pos + 1)
            self.setText(self.history[self.pos] if self.pos < len(self.history) else "")
        else:
            super().keyPressEvent(e)


class ChatPanel(QWidget):
    result_ready = Signal(object)

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.thinking = False

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.input = HistoryLine()
        self.input.setPlaceholderText("Напиши команду: «пройди два метра вперёд и повернись налево»")
        self.send_btn = QPushButton("Отправить")
        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setToolTip("Остановить робота (без Qwen). Ещё: напиши «стоп» или нажми Esc")
        self.state = QLabel("")

        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        row.addWidget(self.stop_btn)
        quick = QHBoxLayout()
        for text in QUICK:
            b = QPushButton(text)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, t=text: self.send(t))
            quick.addWidget(b)
        quick.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(quick)
        lay.addLayout(row)
        lay.addWidget(self.state)

        self.send_btn.clicked.connect(lambda: self.send(self.input.text()))
        self.input.returnPressed.connect(lambda: self.send(self.input.text()))
        self.stop_btn.clicked.connect(self.stop)
        self.result_ready.connect(self._on_result)
        self._say("system", "Привет! Пиши команды по-русски — Qwen превратит их в план, а робот выполнит. "
                            "«стоп» останавливает сразу, без Qwen.")

    # ------------------------------------------------------------------
    def _say(self, who: str, text: str, color: str = "") -> None:
        t = time.strftime("%H:%M:%S")
        if who == "you":
            body = f"<b>ты&gt;</b> {esc(text)}"
        elif who == "qwen":
            body = f'<b style="color:{GREEN}">qwen&gt;</b> {esc(text)}'
        elif who == "robot":
            body = f'<span style="color:{color or "#1565c0"}">🤖 {esc(text)}</span>'
        else:
            body = f'<span style="color:{color or GREY}">{esc(text)}</span>'
        self.view.append(f'<span style="color:{GREY}; font-size:small">{t}</span> {body}')
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def send(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.input.remember(text)
        self.input.clear()
        self._say("you", text)
        low = text.lower().strip(" .!")
        from g1_studio.core.planner import STOP_WORDS
        if low in STOP_WORDS:
            self.stop()
            return
        if self.thinking:
            self._say("system", "Qwen ещё думает над прошлой командой — подожди или нажми «Стоп».", YELLOW)
            return
        self.thinking = True
        self.send_btn.setEnabled(False)
        self.state.setText("Qwen думает…")

        def work():
            res = self.studio.command(text)
            self.result_ready.emit(res)

        threading.Thread(target=work, name="qwen-command", daemon=True).start()

    def stop(self) -> None:
        self.studio.stop()
        self._say("robot", "■ стоп", RED)

    def _on_result(self, res) -> None:
        self.thinking = False
        self.send_btn.setEnabled(True)
        self.state.setText("")
        if res.stopped:
            self._say("robot", "■ стоп", RED)
            return
        if res.error:
            self._say("system", f"✗ Qwen: {res.error}", RED)
        # ответ Qwen показывается по событию «reply» — оно приходит раньше шагов плана

    # --- события исполнителя (из MainWindow) ---
    def on_event(self, kind: str, data: dict) -> None:
        if kind == "reply":
            self._say("qwen", f"{data['text'] or '(без ответа)'}  ({data['seconds']:.1f} с)")
            for w in data["warnings"]:
                self._say("system", f"! {w}", YELLOW)
        elif kind == "plan":
            steps = data["steps"]
            self._say("robot", "план: " + " → ".join(steps) if len(steps) <= 4
                      else f"план из {len(steps)} шагов: " + " → ".join(steps))
        elif kind == "step":
            self._say("robot", f"[{data['i']}/{data['n']}] {data['text']}")
            self.state.setText(f"Выполняю [{data['i']}/{data['n']}] {data['text']}")
        elif kind == "info":
            self._say("robot", data["text"])
        elif kind == "warn":
            self._say("robot", "! " + data["text"], YELLOW)
        elif kind == "done":
            self.state.setText("")
            if data["ok"]:
                self._say("robot", "✓ готово", GREEN)
            elif data.get("cancelled"):
                self._say("robot", "план прерван", GREY)
            else:
                self._say("robot", "✗ план не выполнен до конца", RED)
