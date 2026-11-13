"""Окно целиком, без экрана (QT_QPA_PLATFORM=offscreen): собирается, чат и кнопки работают."""
import gc
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from g1_studio.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(app, fake_llm, sim_repo):
    from g1_studio.gui.main_window import MainWindow
    cfg = Config.load()
    cfg.qwen.url = fake_llm.url
    cfg.sim.repo = str(sim_repo)
    cfg.camera.enabled = False
    w = MainWindow(cfg, dry=True)
    w.show()
    yield w
    w.close()
    # Удаляем окно сейчас и в главном потоке: иначе сборщик мусора может удалить
    # Qt-объекты из фонового потока — это падение Qt, а не ошибка программы.
    w.deleteLater()
    pump(app, 0.1)
    del w
    gc.collect()


def test_window_builds(app, window):
    pump(app, 0.5)
    assert window.tabs.count() == 4
    assert "без сима" in window.status.dds.text()


def test_chat_runs_plan(app, window, fake_llm):
    fake_llm.plans["повернись"] = {"reply": "Поворачиваю", "steps": [{"action": "wait", "value": 0.2}]}
    window.chat.send("повернись")
    t0 = time.monotonic()
    while "готово" not in window.chat.view.toPlainText() and time.monotonic() - t0 < 10:
        pump(app, 0.05)
    text = window.chat.view.toPlainText()
    assert text.index("Поворачиваю") < text.index("[1/1]") < text.index("готово")   # ответ раньше шагов


def test_chat_stop_word_skips_qwen(app, window, fake_llm):
    n = len(fake_llm.requests)
    window.chat.send("стоп")
    pump(app, 0.2)
    assert len(fake_llm.requests) == n and "стоп" in window.chat.view.toPlainText()


def test_keyboard_drive_ru_layout(app, window):
    window.activateWindow()
    window.drive.setFocus()
    pump(app, 0.1)
    if not window.isActiveWindow():
        pytest.skip("offscreen-платформа не даёт окну фокус")
    QtWidgets.QApplication.sendEvent(window.drive, QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier, "ц"))
    assert window.studio.controller.mode == "manual" and window.studio.controller.target[0] > 0
    QtWidgets.QApplication.sendEvent(window.drive, QKeyEvent(QEvent.KeyRelease, Qt.Key_W, Qt.NoModifier, "ц"))
    assert window.studio.controller.target[0] == 0


def test_hold_buttons(app, window):
    window.drive.buttons["turn_l"].pressed.emit()
    assert window.studio.controller.target[2] > 0
    window.drive.buttons["turn_l"].released.emit()
    assert window.studio.controller.target[2] == 0


def test_settings_dialog_roundtrip(app, window):
    from g1_studio.gui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(window.studio.cfg, window)
    new = dlg.result_config()
    assert not window.studio.cfg.diff(new)              # ничего не трогали — ничего не поменялось
    dlg.tabs.setCurrentIndex(0)
    dlg.reset_tab()
    dlg.deleteLater()


def test_scene_panel_toggles_ball(app, window, sim_repo):
    from g1_studio.sim import scene
    window.scene.refresh()
    assert window.scene.ball_btn.isEnabled()
    window.scene.toggle_ball()
    assert scene.status(sim_repo).ball
    assert window.scene.ball_btn.text() == "Убрать мяч"
