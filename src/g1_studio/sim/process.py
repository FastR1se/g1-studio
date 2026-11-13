"""Запуск Isaac Sim (или MuJoCo-стенда) отдельным процессом в его родном окружении conda.

Приложение живёт в своём окружении (PySide6 не мешает Isaac Sim), а сим запускается как
  conda run --no-capture-output -n unitree_sim python -u sim_main.py ...
в папке unitree_sim_isaaclab. conda run подхватывает переменные окружения env
(CYCLONEDDS_HOME, OMNI_KIT_ACCEPT_EULA, ...), которые выставил установщик.

Остановка: SIGINT всей группе процессов (как Ctrl+C в терминале) -> ждём -> SIGTERM -> SIGKILL.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("g1studio.sim")


class SimError(RuntimeError):
    pass


def find_conda(explicit: str = "") -> str | None:
    cands = [explicit] if explicit else []
    cands += [os.environ.get("CONDA_EXE", ""), shutil.which("conda") or ""]
    home = Path.home()
    for d in ("miniconda3", "anaconda3", "miniforge3", "mambaforge", ".conda"):
        cands.append(str(home / d / "bin" / "conda"))
    cands.append("/opt/conda/bin/conda")
    for c in cands:
        if c and os.path.isfile(os.path.expanduser(c)) and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    return None


def conda_envs(conda: str) -> list[str]:
    """Имена окружений conda (по последней части пути)."""
    try:
        out = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True, timeout=30)
        return [Path(p).name for p in json.loads(out.stdout).get("envs", [])]
    except Exception:
        return []


def sim_command(cfg) -> tuple[list[str], Path, dict]:
    """(команда, рабочая папка, доп. переменные окружения) для запуска сима по настройкам."""
    s = cfg.sim
    env = {"PYTHONUNBUFFERED": "1",
           "G1_BALL": "1" if s.ball else "0", "G1_BALL_POS": s.ball_pos,
           "G1_OBJECTS": "1" if s.objects else "0"}
    if s.backend == "bench":
        cmd = [sys.executable, "-u", "-m", "g1_studio.bench", f"--ball-pos={s.ball_pos}",
               "--port", str(cfg.camera.port), "--domain", str(cfg.dds.domain)]
        return cmd, Path.cwd(), env
    repo = cfg.sim_repo
    if not (repo / "sim_main.py").is_file():
        raise SimError(f"нет {repo / 'sim_main.py'} — укажи папку unitree_sim_isaaclab в настройках")
    conda = find_conda(s.conda)
    if conda is None:
        raise SimError("не нашёл conda — укажи путь в настройках (Симулятор → Путь к conda)")
    args = ["--device", s.device, "--enable_cameras", "--task", s.task, "--robot_type", s.robot_type]
    args += shlex.split(s.extra_args)
    if s.headless and "--headless" not in args:
        args.append("--headless")
    cmd = [conda, "run", "--no-capture-output", "-n", s.conda_env, "python", "-u", "sim_main.py", *args]
    return cmd, repo, env


def env_python_command(cfg, script: str, *args: str) -> list[str]:
    """Запустить свой скрипт в окружении сима (например, поиск предметов в библиотеке Isaac)."""
    conda = find_conda(cfg.sim.conda)
    if conda is None:
        raise SimError("не нашёл conda — укажи путь в настройках")
    return [conda, "run", "--no-capture-output", "-n", cfg.sim.conda_env, "python", "-u", script, *args]


def find_external_sim(own_pgid: int | None = None) -> int | None:
    """PID sim_main.py, запущенного не нами (например, в терминале), или None."""
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        try:
            parts = (d / "cmdline").read_bytes().split(b"\0")
            if not os.path.basename(parts[0]).startswith(b"python"):
                continue                                  # conda run и прочие обёртки — мимо
            if not any(p.endswith(b"sim_main.py") for p in parts[1:3]):
                continue
            if own_pgid is not None and os.getpgid(pid) == own_pgid:
                continue
        except (OSError, IndexError):
            continue
        return pid
    return None


class ManagedProcess:
    """Долгий процесс с выводом построчно в колбэк и аккуратной остановкой всей группы."""

    def __init__(self, name: str, on_line: Callable[[str], None] | None = None,
                 on_exit: Callable[[int], None] | None = None):
        self.name = name
        self.on_line = on_line
        self.on_exit = on_exit
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.returncode: int | None = None
        self.command: list[str] = []
        self._stopping = False
        self._reader: threading.Thread | None = None
        self.tail: list[str] = []            # последние строки вывода — для сообщения об ошибке

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def uptime(self) -> float:
        return time.monotonic() - self.started_at if self.running else 0.0

    def start(self, cmd: list[str], cwd: Path, extra_env: dict | None = None) -> None:
        if self.running:
            raise SimError(f"{self.name} уже запущен (pid {self.proc.pid})")
        env = {**os.environ, **(extra_env or {})}
        # Не тащим в сим переменные Qt/Python нашего окружения: у Isaac Sim свои библиотеки
        for k in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH", "PYTHONPATH", "PYTHONHOME"):
            env.pop(k, None)
        self.command = cmd
        self.tail = []
        self.returncode = None
        self._stopping = False
        log.info("%s: %s  (папка %s)", self.name, shlex.join(cmd), cwd)
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         start_new_session=True, text=True, bufsize=1, errors="replace")
        except OSError as e:
            raise SimError(f"не запустился {cmd[0]}: {e}") from None
        self.started_at = time.monotonic()
        self._reader = threading.Thread(target=self._read, name=f"{self.name}-out", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            self.tail = (self.tail + [line])[-40:]
            if self.on_line:
                try:
                    self.on_line(line)
                except Exception:
                    pass
        rc = proc.wait()
        self.returncode = rc
        if not self._stopping and rc != 0:
            log.error("%s завершился с кодом %s", self.name, rc)
        else:
            log.info("%s завершился (код %s)", self.name, rc)
        if self.on_exit:
            try:
                self.on_exit(rc)
            except Exception:
                pass

    def _signal(self, sig) -> None:
        try:
            os.killpg(self.proc.pid, sig)        # вся группа: conda run + python + дочерние процессы Kit
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, timeout: float = 20.0) -> int | None:
        if not self.running:
            return self.returncode
        self._stopping = True
        log.info("%s: останавливаю (Ctrl+C)…", self.name)
        self._signal(signal.SIGINT)
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("%s не остановился за %.0f с — SIGTERM", self.name, timeout)
            self._signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                log.warning("%s: SIGKILL", self.name)
                self._signal(signal.SIGKILL)
                self.proc.wait(5)
        if self._reader:
            self._reader.join(timeout=2)
        return self.proc.returncode
