"""Запуск окна."""
from __future__ import annotations

import os
import sys


def run(cfg, dry: bool = False) -> int:
    # opencv-python (не headless) подсовывает свои Qt-плагины — убираем, чтобы окно открылось
    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    from PySide6.QtWidgets import QApplication

    from g1_studio import APP_NAME
    from g1_studio.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("g1-studio")
    app.setDesktopFileName("g1-studio")
    win = MainWindow(cfg, dry=dry)
    win.show()
    return app.exec()
