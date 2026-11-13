"""Логи: ~/.local/state/g1-studio/logs/app.log (приложение) и sim.log (вывод сима)."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

SIM_OUT = "g1studio.simout"        # вывод процесса сима — отдельный файл, в общий лог не идёт


def log_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "g1-studio" / "logs"


def setup(verbose: bool = False, console: bool = True) -> Path:
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger("g1studio")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(d / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        if console:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            ch.setLevel(logging.INFO if verbose else logging.WARNING)
            root.addHandler(ch)

    sim = logging.getLogger(SIM_OUT)
    sim.setLevel(logging.INFO)
    sim.propagate = False
    if not sim.handlers:
        sh = logging.handlers.RotatingFileHandler(d / "sim.log", maxBytes=10_000_000, backupCount=2, encoding="utf-8")
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        sim.addHandler(sh)
    return d
