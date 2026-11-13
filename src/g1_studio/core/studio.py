"""Studio — всё приложение без интерфейса: связь с симом, камера, Qwen, исполнитель, процесс сима.

Им пользуются и окно (gui), и терминальный чат (g1-studio chat). События приходят из фоновых
потоков через on_event(kind, data): окно перекладывает их в поток интерфейса.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from g1_studio.config import Config, restart_needed
from g1_studio.core.controller import Controller
from g1_studio.core.link import DryLink, LinkUnavailable, SimLink
from g1_studio.core.planner import STOP_WORDS, LLMError, QwenPlanner, Step, validate_plan
from g1_studio.logs import SIM_OUT
from g1_studio.sim import scene
from g1_studio.sim.process import ManagedProcess, SimError, env_python_command, find_external_sim, sim_command

log = logging.getLogger("g1studio")
sim_out = logging.getLogger(SIM_OUT)


@dataclass
class CommandResult:
    text: str
    reply: str = ""
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    stopped: bool = False


class Studio:
    QWEN_CHECK_EVERY = 20.0

    def __init__(self, cfg: Config, dry: bool = False,
                 on_event: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        self.dry = dry
        self.on_event = on_event
        self.link = None
        self.link_note = ""
        self.camera = None
        self.camera_note = ""
        self.yolo_note = ""
        self.controller: Controller | None = None
        self.planner: QwenPlanner | None = None
        self.planner_error = ""
        self.qwen_ok: bool | None = None
        self.qwen_text = "проверяю…"
        self._qwen_wake = threading.Event()
        self._alive = True
        self.sim = ManagedProcess("сим", on_line=self._sim_line, on_exit=self._sim_exit)
        self.tool = ManagedProcess("поиск предметов", on_line=self._tool_line, on_exit=self._tool_exit)
        self._ext_pid: int | None = None
        self._ext_checked = 0.0
        self._plan_lock = threading.Lock()

    # ------------------------------------------------------------------
    def emit(self, kind: str, **data) -> None:
        if self.on_event:
            try:
                self.on_event(kind, data)
            except Exception:
                log.exception("обработчик события %s", kind)

    def start(self) -> None:
        self._connect_link()
        self._connect_camera()
        self.controller = Controller(self.link, self.camera, self.cfg.robot.sim_speed,
                                     on_event=lambda kind, data: self.emit(kind, **data))
        self._make_planner()
        threading.Thread(target=self._qwen_loop, name="qwen-check", daemon=True).start()

    def _connect_link(self) -> None:
        if self.dry:
            self.link, self.link_note = DryLink("режим без сима"), "режим без сима (--dry)"
            return
        try:
            d = self.cfg.dds
            self.link = SimLink(d.domain, d.interface or None, d.quat_order)
            self.link_note = ""
            log.info("DDS: domain %s, жду rt/lowstate", self.link.domain)
        except LinkUnavailable as e:
            self.link, self.link_note = DryLink(str(e)), str(e)
            log.warning("DDS недоступен: %s", e)

    def _connect_camera(self) -> None:
        if self.camera is not None:
            self.camera.close()
            self.camera = None
        if not self.cfg.camera.enabled or self.dry:
            self.camera_note = "камера выключена" if not self.dry else "режим без сима"
            return
        try:
            from g1_studio.vision.stream import CameraStream
            self.camera = CameraStream(self.cfg.camera.host, self.cfg.camera.port, yolo=self._load_yolo())
            self.camera_note = ""
        except ImportError as e:
            self.camera_note = f"зрение не загрузилось: {e}"
            log.error(self.camera_note)
        if self.controller is not None:
            self.controller.camera = self.camera

    def _load_yolo(self):
        if not self.cfg.yolo.enabled:
            self.yolo_note = "YOLO выключен"
            return None
        from g1_studio.vision.stream import load_yolo
        y, self.yolo_note = load_yolo(self.cfg.yolo.model, self.cfg.yolo.conf, self.cfg.yolo.threads)
        (log.info if y else log.warning)(self.yolo_note)
        return y

    def _make_planner(self) -> None:
        try:
            self.planner = QwenPlanner.from_config(self.cfg)
            self.planner_error = ""
        except LLMError as e:
            self.planner, self.planner_error = None, str(e)
            log.error("Qwen: %s", e)
        self.qwen_ok, self.qwen_text = None, "проверяю…"
        self._qwen_wake.set()

    # --- Qwen: фоновая проверка доступности ---
    def check_qwen(self) -> None:
        self._qwen_wake.set()

    def _qwen_loop(self) -> None:
        while self._alive:
            planner = self.planner
            if planner is None:
                ok, text = False, self.planner_error or "Qwen не настроен"
            else:
                try:
                    planner.check()
                    ok, text = True, f"{planner.model} ({planner.where})"
                except LLMError as e:
                    ok, text = False, str(e)
            if (ok, text) != (self.qwen_ok, self.qwen_text):
                self.qwen_ok, self.qwen_text = ok, text
                (log.info if ok else log.warning)("Qwen: %s", text)
                self.emit("qwen_status", ok=ok, text=text)
            self._qwen_wake.wait(self.QWEN_CHECK_EVERY)
            self._qwen_wake.clear()

    # ------------------------------------------------------------------
    # Настройки на ходу
    # ------------------------------------------------------------------
    def apply_config(self, new: Config) -> set[str]:
        """Применить новые настройки. Возвращает, что ещё надо перезапустить: {"app", "sim"}."""
        changed = self.cfg.diff(new)
        if not changed:
            return set()
        old = self.cfg
        self.cfg = new
        keys = {f"{s}.{k}" for s, k in changed}
        log.info("Настройки изменены: %s", ", ".join(sorted(keys)))
        if any(k.startswith("qwen.") for k in keys):
            self._make_planner()
        if any(k.startswith("camera.") for k in keys):
            self._connect_camera()
        elif any(k.startswith("yolo.") for k in keys) and self.camera is not None:
            if keys <= {"yolo.conf"} and self.camera.yolo is not None:
                self.camera.yolo.conf = new.yolo.conf
            else:
                self.camera.set_yolo(self._load_yolo())
        if self.controller is not None:
            self.controller.sim_speed = new.robot.sim_speed
        if isinstance(self.link, SimLink):
            self.link.quat_order = new.dds.quat_order
        need = restart_needed(changed)
        if "sim" in need and not self.sim.running:
            need.discard("sim")
        if "app" in need and old.dds.domain == new.dds.domain and old.dds.interface == new.dds.interface:
            need.discard("app")
        return need

    def save_config(self) -> None:
        try:
            self.cfg.save()
        except OSError as e:
            log.error("Не сохранил настройки: %s", e)

    def set_yolo(self, enabled: bool | None = None, conf: float | None = None) -> None:
        new = self.cfg.copy()
        if enabled is not None:
            new.yolo.enabled = enabled
        if conf is not None:
            new.yolo.conf = round(conf, 2)
        self.apply_config(new)
        self.save_config()

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    def command(self, text: str) -> CommandResult:
        """Текст -> Qwen -> план -> исполнение (в фоне). Блокирует на время ответа Qwen."""
        text = text.strip()
        res = CommandResult(text)
        if text.lower().strip(" .!") in STOP_WORDS:
            self.stop()
            res.stopped = True
            return res
        planner = self.planner
        if planner is None:
            res.error = self.planner_error or "Qwen не настроен"
            return res
        t0 = time.monotonic()
        with self._plan_lock:                     # одна команда за раз — Qwen на CPU не любит очереди
            try:
                plan = planner.plan(text)
            except LLMError as e:
                res.error = str(e)
                self.qwen_ok = False
                self.check_qwen()
                return res
        res.seconds = time.monotonic() - t0
        res.reply, res.steps, res.warnings = validate_plan(plan)
        log.info("Qwen (%.1f с): «%s» -> %s", res.seconds, text, res.reply or "(без ответа)")
        for w in res.warnings:
            log.warning("план: %s", w)
        self.emit("reply", text=res.reply, seconds=res.seconds, warnings=res.warnings)   # до плана — для порядка в чате
        if res.steps and self.controller is not None:
            self.controller.run(res.steps)        # новая команда прерывает текущий план
        return res

    def stop(self) -> None:
        if self.controller is not None:
            self.controller.stop()
        log.info("■ стоп")

    def emergency_stop(self) -> None:
        if self.controller is not None:
            self.controller.emergency_stop()

    # ------------------------------------------------------------------
    # Процесс сима
    # ------------------------------------------------------------------
    def start_sim(self) -> None:
        ext = find_external_sim()
        if ext and not self.sim.running:
            raise SimError(f"сим уже запущен вне приложения (pid {ext}) — к нему можно просто подключиться")
        cmd, cwd, env = sim_command(self.cfg)
        sim_out.info("===== запуск: %s", " ".join(cmd))
        self.sim.start(cmd, cwd, env)
        self.emit("sim_state", running=True)

    def stop_sim(self) -> None:
        self.stop()
        self.sim.stop(self.cfg.sim.stop_timeout)
        self.emit("sim_state", running=False)

    def _sim_line(self, line: str) -> None:
        sim_out.info(line)
        self.emit("sim_line", line=line)

    def _sim_exit(self, code: int) -> None:
        sim_out.info("===== сим завершился, код %s", code)
        crashed = code not in (0, None) and not self.sim._stopping
        self.emit("sim_exit", code=code, crashed=crashed, tail=list(self.sim.tail[-15:]))

    def external_sim_pid(self) -> int | None:
        """sim_main.py, запущенный не из приложения (проверка не чаще раза в 3 с)."""
        if time.monotonic() - self._ext_checked > 3:
            self._ext_checked = time.monotonic()
            own = None
            if self.sim.running:
                try:
                    import os
                    own = os.getpgid(self.sim.pid)
                except OSError:
                    own = None
            self._ext_pid = find_external_sim(own)
        return self._ext_pid

    # --- поиск предметов в библиотеке Isaac (долго, в окружении сима) ---
    def find_objects(self) -> None:
        repo = self.cfg.sim_repo
        cmd = env_python_command(self.cfg, str(scene.find_objects_script()), "--out", str(repo / scene.OBJECTS_JSON))
        sim_out.info("===== поиск предметов: %s", " ".join(cmd))
        self.tool.start(cmd, repo, {"PYTHONUNBUFFERED": "1"})

    def _tool_line(self, line: str) -> None:
        sim_out.info("[поиск] %s", line)
        self.emit("tool_line", line=line)

    def _tool_exit(self, code: int) -> None:
        self.emit("tool_exit", code=code)

    # ------------------------------------------------------------------
    def status(self) -> dict:
        link, cam, ctl = self.link, self.camera, self.controller
        yaw = link.yaw() if link else None
        st = {
            "link_kind": link.kind if link else "—",
            "link_note": self.link_note,
            "state_age": link.state_age() if link else float("inf"),
            "state_rate": link.state_rate() if link else 0.0,
            "yaw_deg": None if yaw is None else round(yaw * 57.29578, 1),
            "camera": cam is not None,
            "camera_note": self.camera_note,
            "camera_addr": cam.address if cam else "",
            "camera_alive": bool(cam and cam.alive()),
            "camera_fps": cam.fps() if cam else 0.0,
            "yolo": bool(cam and cam.yolo),
            "yolo_model": cam.yolo.model_name if cam and cam.yolo else "",
            "yolo_ms": cam.yolo_ms if cam else 0.0,
            "yolo_note": self.yolo_note,
            "qwen_ok": self.qwen_ok,
            "qwen_text": self.qwen_text,
            "sim_running": self.sim.running,
            "sim_pid": self.sim.pid if self.sim.running else None,
            "sim_uptime": self.sim.uptime(),
            "sim_external": self.external_sim_pid(),
            "tool_running": self.tool.running,
        }
        st.update(ctl.status() if ctl else {"mode": "idle", "step": "", "cmd": [0, 0, 0, 0.8],
                                            "target": [0, 0, 0, 0.8], "sim_speed_est": None})
        return st

    def shutdown(self) -> None:
        self._alive = False
        self._qwen_wake.set()
        if self.controller is not None:
            self.controller.shutdown()
        if self.camera is not None:
            self.camera.close()
        if self.tool.running:
            self.tool.stop(5)
        if self.sim.running:
            self.sim.stop(self.cfg.sim.stop_timeout)
