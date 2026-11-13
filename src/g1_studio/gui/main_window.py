"""Главное окно G1 Studio."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox, QSizePolicy, QSplitter, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from g1_studio import APP_NAME, __version__, logs
from g1_studio.core.studio import Studio
from g1_studio.gui.camera_panel import CameraPanel
from g1_studio.gui.chat_panel import ChatPanel
from g1_studio.gui.common import Bridge, QtLogHandler
from g1_studio.gui.drive_panel import DrivePanel, KeyboardFilter
from g1_studio.gui.log_panel import LogPanel
from g1_studio.gui.scene_panel import ScenePanel
from g1_studio.gui.settings_dialog import SettingsDialog
from g1_studio.gui.status_panel import StatusPanel
from g1_studio.sim.process import SimError

log = logging.getLogger("g1studio.gui")


class MainWindow(QMainWindow):
    def __init__(self, cfg, dry: bool = False):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1360, 820)
        self.bridge = Bridge()
        self._log_handler = QtLogHandler(self.bridge)
        logging.getLogger("g1studio").addHandler(self._log_handler)
        self.bridge.event.connect(self.on_event)

        self.studio = Studio(cfg, dry=dry, on_event=self.bridge.event.emit)
        self.studio.start()

        # --- панели ---
        self.camera = CameraPanel(self.studio)
        self.status = StatusPanel()
        self.chat = ChatPanel(self.studio)
        self.drive = DrivePanel(self.studio)
        self.scene = ScenePanel(self.studio)
        self.logs = LogPanel()
        self.bridge.log_record.connect(self.logs.add)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.chat, "Чат с Qwen")
        self.tabs.addTab(self.drive, "Управление")
        self.tabs.addTab(self.scene, "Сцена")
        self.tabs.addTab(self.logs, "Логи")

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.status)
        rl.addWidget(self.tabs, 1)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.camera)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        self.setCentralWidget(split)

        self._toolbar()
        self.status_line = QLabel()
        self.statusBar().addWidget(self.status_line, 1)

        self.keys = KeyboardFilter(self.drive, self)
        QApplication.instance().installEventFilter(self.keys)
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.setContext(Qt.ApplicationShortcut)
        esc.activated.connect(self.emergency_stop)

        self.status.apply_sim_speed.connect(self.apply_sim_speed)
        self.scene.restart_sim.connect(self.restart_sim)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)
        self.tick()
        log.info("%s %s запущен. Настройки: %s", APP_NAME, __version__, cfg.path)

    def _toolbar(self) -> None:
        tb = QToolBar("Главная")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.act_start = QAction("▶ Запустить сим", self)
        self.act_start.setToolTip("Запустить Isaac Sim (или MuJoCo-стенд) по настройкам")
        self.act_stop = QAction("■ Остановить сим", self)
        self.act_reconnect = QAction("⟳ Переподключиться", self)
        self.act_reconnect.setToolTip("Камера и Qwen заново. Сим, запущенный в терминале, подхватывается сам.")
        self.act_settings = QAction("Настройки…", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_estop = QAction("⛔ СТОП робота (Esc)", self)
        self.act_estop.setToolTip("Аварийная остановка: прервать всё и обнулить скорость")
        tb.addAction(self.act_start)
        tb.addAction(self.act_stop)
        tb.addAction(self.act_reconnect)
        tb.addSeparator()
        tb.addAction(self.act_settings)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_estop)
        self.act_start.triggered.connect(self.start_sim)
        self.act_stop.triggered.connect(self.stop_sim)
        self.act_reconnect.triggered.connect(self.reconnect)
        self.act_settings.triggered.connect(self.open_settings)
        self.act_estop.triggered.connect(self.emergency_stop)

        menu = self.menuBar().addMenu("Файл")
        menu.addAction(self.act_settings)
        menu.addSeparator()
        menu.addAction("Выход", self.close)
        about = self.menuBar().addMenu("Справка")
        about.addAction("О программе", self.about)

    # ------------------------------------------------------------------
    def tick(self) -> None:
        st = self.studio.status()
        self.status.update_status(st, self.studio.cfg)
        running = st["sim_running"]
        self.act_start.setEnabled(not running)
        self.act_stop.setEnabled(running)
        self.scene.restart_btn.setVisible(running)
        bits = [f"Сим: {'работает' if st['state_age'] < 1 else 'нет связи'}",
                f"камера {st['camera_fps']:.0f} к/с" if st["camera_alive"] else "камера: нет кадров",
                "Qwen ✓" if st["qwen_ok"] else "Qwen ✗"]
        self.status_line.setText("   ·   ".join(bits))

    def on_event(self, kind: str, data: dict) -> None:
        if kind in ("reply", "plan", "step", "info", "warn", "done"):
            self.chat.on_event(kind, data)
        elif kind == "sim_line":
            self.logs.add("сим", logging.INFO, data["line"])
        elif kind == "tool_line":
            self.logs.add("поиск", logging.INFO, data["line"])
        elif kind == "tool_exit":
            self.scene.refresh()
            if data["code"] == 0:
                QMessageBox.information(self, "Поиск предметов", "Готово: список предметов обновлён.\n"
                                        "Подключи предметы (если ещё не) и перезапусти сим.")
            else:
                QMessageBox.warning(self, "Поиск предметов", f"Поиск завершился с ошибкой (код {data['code']}). "
                                    "Подробности — во вкладке «Логи».")
        elif kind == "sim_exit":
            if data["crashed"]:
                tail = "\n".join(data["tail"][-12:])
                QMessageBox.warning(self, "Сим упал", f"Сим завершился с кодом {data['code']}.\n\n"
                                    f"Последние строки:\n{tail}\n\nПолный вывод — вкладка «Логи» (Сим) и {logs.log_dir()}/sim.log")
        elif kind == "sim_speed_hint":
            self.statusBar().showMessage(f"Сим идёт ~{data['value']}× от реального времени — "
                                         "нажми «Применить» в «Состоянии»", 15000)

    # ------------------------------------------------------------------
    def start_sim(self) -> None:
        try:
            self.studio.start_sim()
        except SimError as e:
            QMessageBox.warning(self, "Запуск сима", str(e))
            return
        self.tabs.setCurrentWidget(self.logs)
        self.logs.source.setCurrentIndex(0)

    def stop_sim(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.studio.stop_sim()
        finally:
            QApplication.restoreOverrideCursor()

    def restart_sim(self) -> None:
        self.stop_sim()
        self.start_sim()

    def reconnect(self) -> None:
        self.studio._connect_camera()
        self.studio.check_qwen()
        self.camera.sync_from_config()
        log.info("Переподключился: камера %s", self.studio.camera.address if self.studio.camera else "выключена")

    def emergency_stop(self) -> None:
        self.drive.release_all()
        self.studio.emergency_stop()

    def apply_sim_speed(self, value: float) -> None:
        new = self.studio.cfg.copy()
        new.robot.sim_speed = value
        self.studio.apply_config(new)
        self.studio.save_config()
        log.info("Скорость сима: %.2f×", value)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.studio.cfg, self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        need = self.studio.apply_config(dlg.result_config())
        self.studio.save_config()
        self.camera.sync_from_config()
        self.camera.set_fps(self.studio.cfg.ui.camera_fps)
        self.scene.refresh()
        if "sim" in need:
            if QMessageBox.question(self, "Настройки", "Часть настроек вступит в силу после перезапуска сима. "
                                    "Перезапустить сейчас?") == QMessageBox.Yes:
                self.restart_sim()
        if "app" in need:
            QMessageBox.information(self, "Настройки", "Смена DDS domain/интерфейса вступит в силу после "
                                    "перезапуска G1 Studio.")

    def about(self) -> None:
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME} {__version__}</b><br>Пульт для Unitree G1 в Isaac Sim:<br>"
                          "камера с YOLO, чат с Qwen, ручное управление.<br><br>"
                          f"Настройки: {self.studio.cfg.path}<br>Логи: {logs.log_dir()}")

    # ------------------------------------------------------------------
    def closeEvent(self, e) -> None:
        if self.studio.sim.running:
            ans = QMessageBox.question(self, "Выход", "Сим запущен из G1 Studio — при выходе он остановится. Выйти?\n\n"
                                       "(Чтобы сим жил дольше приложения, запускай sim_main.py в терминале — "
                                       "G1 Studio подключится к нему сама.)")
            if ans != QMessageBox.Yes:
                e.ignore()
                return
        self.timer.stop()
        self.camera.timer.stop()
        self.studio.on_event = None              # фоновые потоки больше не пишут в закрытое окно
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.studio.shutdown()
        finally:
            QApplication.restoreOverrideCursor()
        logging.getLogger("g1studio").removeHandler(self._log_handler)
        QApplication.instance().removeEventFilter(self.keys)
        e.accept()
