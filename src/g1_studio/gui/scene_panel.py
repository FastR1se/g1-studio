"""Сцена: мяч и предметы для YOLO в задаче Isaac-Move-Cylinder."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from g1_studio.sim import scene
from g1_studio.sim.process import SimError

log = logging.getLogger("g1studio.scene")


class ScenePanel(QWidget):
    restart_sim = Signal()

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio

        self.where = QLabel()
        self.where.setWordWrap(True)
        self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # --- мяч ---
        ball = QGroupBox("Красный мяч (∅ 20 см, его можно толкать)")
        self.ball_state = QLabel()
        self.ball_btn = QPushButton()
        self.ball_on = QCheckBox("Показывать при запуске")
        self.ball_pos = QLineEdit()
        self.ball_pos.setPlaceholderText("-2.7,-2.0")
        self.ball_pos.setToolTip("x,y в метрах в системе сцены. Робот стоит в (-3.9, -2.818) лицом по +x.")
        bl = QVBoxLayout(ball)
        r1 = QHBoxLayout()
        r1.addWidget(self.ball_state, 1)
        r1.addWidget(self.ball_btn)
        r2 = QHBoxLayout()
        r2.addWidget(self.ball_on)
        r2.addWidget(QLabel("Позиция x,y:"))
        r2.addWidget(self.ball_pos)
        bl.addLayout(r1)
        bl.addLayout(r2)

        # --- предметы ---
        objs = QGroupBox("Предметы для YOLO (человек, стул, бутылка, кружка…)")
        self.obj_state = QLabel()
        self.obj_state.setWordWrap(True)
        self.obj_btn = QPushButton()
        self.obj_on = QCheckBox("Показывать при запуске")
        self.find_btn = QPushButton("Найти предметы в библиотеке Isaac…")
        self.find_btn.setToolTip("Ищет модели в облачной библиотеке NVIDIA, меряет и расставляет вокруг робота. "
                                 "2–10 минут, нужен интернет. Результат — scene_objects.json")
        self.edit_btn = QPushButton("Открыть scene_objects.json")
        ol = QVBoxLayout(objs)
        r3 = QHBoxLayout()
        r3.addWidget(self.obj_state, 1)
        r3.addWidget(self.obj_btn)
        r4 = QHBoxLayout()
        r4.addWidget(self.obj_on)
        r4.addStretch(1)
        r4.addWidget(self.find_btn)
        r4.addWidget(self.edit_btn)
        ol.addLayout(r3)
        ol.addLayout(r4)

        self.note = QLabel("Изменения сцены вступают при следующем запуске сима.")
        self.restart_btn = QPushButton("Перезапустить сим")
        nl = QHBoxLayout()
        nl.addWidget(self.note, 1)
        nl.addWidget(self.restart_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.where)
        lay.addWidget(ball)
        lay.addWidget(objs)
        lay.addLayout(nl)
        lay.addStretch(1)

        self.ball_btn.clicked.connect(self.toggle_ball)
        self.obj_btn.clicked.connect(self.toggle_objects)
        self.find_btn.clicked.connect(self.find_objects)
        self.edit_btn.clicked.connect(self.open_json)
        self.restart_btn.clicked.connect(self.restart_sim.emit)
        self.ball_on.toggled.connect(lambda v: self._set_cfg(ball=v))
        self.obj_on.toggled.connect(lambda v: self._set_cfg(objects=v))
        self.ball_pos.editingFinished.connect(self._ball_pos_changed)
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        cfg = self.studio.cfg
        st = scene.status(cfg.sim_repo)
        self._st = st
        bench = cfg.sim.backend == "bench"
        if st.error:
            self.where.setText(f"✗ {st.error}" + ("\n(Сейчас выбран MuJoCo-стенд — там мяч есть всегда.)" if bench else ""))
        else:
            self.where.setText(f"Файл сцены: {st.task_file}")
        ok = not st.error
        self.ball_state.setText("Мяч добавлен в сцену" if st.ball else "Мяча в сцене нет")
        self.ball_btn.setText("Убрать мяч" if st.ball else "Добавить мяч")
        self.ball_btn.setEnabled(ok)
        n = len(st.objects_list)
        lst = f"{n} шт.: {', '.join(st.objects_list)}" if n else "списка ещё нет — нажми «Найти предметы»"
        self.obj_state.setText(("Предметы подключены. " if st.objects else "Предметы не подключены. ") + lst)
        self.obj_btn.setText("Отключить предметы" if st.objects else "Подключить предметы")
        self.obj_btn.setEnabled(ok)
        self.find_btn.setEnabled(ok and not self.studio.tool.running)
        self.find_btn.setText("Ищу предметы… (см. «Логи»)" if self.studio.tool.running
                              else "Найти предметы в библиотеке Isaac…")
        self.edit_btn.setEnabled(bool(st.objects_json and st.objects_json.is_file()))
        for w, v in ((self.ball_on, cfg.sim.ball), (self.obj_on, cfg.sim.objects)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        if not self.ball_pos.hasFocus():
            self.ball_pos.setText(cfg.sim.ball_pos)
        self.restart_btn.setVisible(self.studio.sim.running)

    def _set_cfg(self, **kw) -> None:
        new = self.studio.cfg.copy()
        for k, v in kw.items():
            setattr(new.sim, k, v)
        self.studio.apply_config(new)
        self.studio.save_config()

    def _ball_pos_changed(self) -> None:
        text = self.ball_pos.text().replace(" ", "")
        try:
            x, y = (float(v) for v in text.split(","))
        except ValueError:
            QMessageBox.warning(self, "Позиция мяча", "Нужно два числа через запятую, например -2.7,-2.0")
            self.ball_pos.setText(self.studio.cfg.sim.ball_pos)
            return
        if text != self.studio.cfg.sim.ball_pos:
            self._set_cfg(ball_pos=f"{x:g},{y:g}")

    def _patch(self, fn, *args) -> None:
        try:
            changed = fn(self.studio.cfg.sim_repo, *args)
            log.info("Сцена: %s", "изменена" if changed else "без изменений")
        except (scene.SceneError, OSError, SyntaxError) as e:
            QMessageBox.warning(self, "Сцена", str(e))
        self.refresh()

    def toggle_ball(self) -> None:
        self._patch(scene.set_ball, not self._st.ball)

    def toggle_objects(self) -> None:
        if not self._st.objects and not self._st.objects_list:
            QMessageBox.information(self, "Предметы", "Сначала найди предметы — кнопка «Найти предметы в библиотеке "
                                    "Isaac». Подключить можно и сейчас: сцена подхватит список, когда он появится.")
        self._patch(scene.set_objects, not self._st.objects)

    def find_objects(self) -> None:
        if QMessageBox.question(self, "Поиск предметов",
                                "Запустить поиск моделей в библиотеке Isaac Sim?\n\n"
                                "Это 2–10 минут, нужен интернет. Isaac Sim поднимется без окна в окружении "
                                f"«{self.studio.cfg.sim.conda_env}». Ход поиска — во вкладке «Логи».") != QMessageBox.Yes:
            return
        try:
            self.studio.find_objects()
        except SimError as e:
            QMessageBox.warning(self, "Поиск предметов", str(e))
        self.refresh()

    def open_json(self) -> None:
        if self._st.objects_json:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._st.objects_json)))
