"""Единый файл настроек: ~/.config/g1-studio/config.toml (или $G1_STUDIO_CONFIG).

Файл создаётся при первом запуске со всеми полями и комментариями — его можно править руками
или через окно «Настройки». Неизвестные поля игнорируются, кривые значения заменяются
на значения по умолчанию (с предупреждением в лог).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("g1studio.config")


def F(default: Any, label: str, help: str = "", choices: list | None = None, kind: str | None = None,
      minimum: float | None = None, maximum: float | None = None, restart: str = ""):
    """Поле настроек с подписью для окна «Настройки» и комментарием для TOML.

    kind: "dir" / "file" — путь (в окне появится кнопка «Обзор»), "secret" — скрытый ввод.
    restart: что нужно перезапустить, чтобы поле вступило в силу ("app" или "sim").
    """
    meta = {"label": label, "help": help, "choices": choices, "kind": kind,
            "min": minimum, "max": maximum, "restart": restart}
    return field(default=default, metadata=meta)


@dataclass
class SimCfg:
    backend: str = F("isaac", "Что запускать", "isaac — Isaac Sim (unitree_sim_isaaclab); bench — лёгкий "
                     "MuJoCo-стенд для проверки приложения без Isaac", choices=["isaac", "bench"], restart="sim")
    repo: str = F("~/unitree_ws/unitree_sim_isaaclab", "Папка unitree_sim_isaaclab", kind="dir", restart="sim")
    conda: str = F("", "Путь к conda", "пусто — найти самому ($CONDA_EXE, PATH, ~/miniconda3)", kind="file",
                   restart="sim")
    conda_env: str = F("unitree_sim", "Окружение conda с Isaac Sim", restart="sim")
    task: str = F("Isaac-Move-Cylinder-G129-Dex1-Wholebody", "Задача (сцена)", restart="sim")
    device: str = F("cpu", "Устройство физики", "cpu — стабильнее для ходьбы (рендер всё равно на GPU); "
                    "cuda — быстрее физика, но больше VRAM", choices=["cpu", "cuda"], restart="sim")
    robot_type: str = F("g129", "Тип робота", restart="sim")
    extra_args: str = F("--enable_dex1_dds", "Доп. аргументы sim_main.py", restart="sim")
    headless: bool = F(False, "Без окна Isaac Sim", "экономит видеопамять; картинка всё равно идёт в G1 Studio",
                       restart="sim")
    ball: bool = F(True, "Мяч в сцене", "работает, если мяч добавлен во вкладке «Сцена»", restart="sim")
    ball_pos: str = F("-2.7,-2.0", "Позиция мяча x,y (м)", "робот стартует в (-3.9, -2.818) лицом по +x",
                      restart="sim")
    objects: bool = F(True, "Предметы в сцене", "работает, если предметы подключены во вкладке «Сцена»",
                      restart="sim")
    stop_timeout: float = F(20.0, "Сколько ждать остановки сима, с", minimum=3, maximum=120)


@dataclass
class DdsCfg:
    domain: int = F(1, "DDS domain", "в симе Unitree — 1, на реальном роботе — 0", minimum=0, maximum=232,
                    restart="app")
    interface: str = F("", "Сетевой интерфейс DDS", "пусто — по умолчанию (для сима так и надо)", restart="app")
    quat_order: str = F("xyzw", "Порядок кватерниона IMU", "сим Unitree — xyzw, реальный G1 — wxyz",
                        choices=["xyzw", "wxyz"])


@dataclass
class CameraCfg:
    enabled: bool = F(True, "Камера включена")
    host: str = F("127.0.0.1", "Адрес камеры (ZMQ)")
    port: int = F(55555, "Порт камеры", minimum=1, maximum=65535)


@dataclass
class YoloCfg:
    enabled: bool = F(True, "YOLO включён")
    model: str = F("", "Модель ONNX", "пусто — models/yolo26s.onnx (или yolo26n.onnx)", kind="file")
    conf: float = F(0.35, "Порог уверенности", minimum=0.05, maximum=0.95)
    threads: int = F(4, "Потоков CPU для YOLO", "остальные ядра остаются симу", minimum=1, maximum=32)


@dataclass
class QwenCfg:
    backend: str = F("ollama", "Где работает Qwen", "ollama — локально; api — OpenAI-совместимый сервер",
                     choices=["ollama", "api"])
    url: str = F("http://localhost:11434", "Адрес сервера",
                 "Ollama: http://localhost:11434; DashScope: https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    model: str = F("qwen3:1.7b", "Модель", "Ollama: qwen3:1.7b / qwen3:4b; API: qwen-plus")
    api_key: str = F("", "Ключ API", "лучше задать переменной QWEN_API_KEY — тогда он не хранится в файле",
                     kind="secret")
    extra_body: str = F("", "Доп. поля запроса (JSON)", 'для Qwen3 на DashScope: {"enable_thinking": false}')
    gpu: bool = F(False, "Ollama на видеокарте", "по умолчанию CPU: видеопамять нужна Isaac Sim")
    timeout: float = F(60.0, "Таймаут ответа, с", minimum=5, maximum=600)


@dataclass
class RobotCfg:
    sim_speed: float = F(1.0, "Скорость сима (× реального времени)",
                         "если сим идёт медленнее, шаги «на N метров» короче; приложение подскажет значение",
                         minimum=0.1, maximum=1.5)
    manual_speed: float = F(0.5, "Ручное: скорость вперёд, м/с", minimum=0.1, maximum=1.0)
    manual_strafe: float = F(0.3, "Ручное: скорость вбок, м/с", minimum=0.1, maximum=0.5)
    manual_turn: float = F(45.0, "Ручное: поворот, °/с", minimum=10, maximum=90)


@dataclass
class UiCfg:
    camera_fps: int = F(15, "Частота обновления камеры в окне", minimum=1, maximum=60)
    keyboard: bool = F(True, "Управление с клавиатуры (WASD, Q/E)")


SECTIONS = {
    "sim": ("Симулятор", SimCfg),
    "dds": ("Связь (DDS)", DdsCfg),
    "camera": ("Камера", CameraCfg),
    "yolo": ("YOLO", YoloCfg),
    "qwen": ("Qwen", QwenCfg),
    "robot": ("Робот", RobotCfg),
    "ui": ("Интерфейс", UiCfg),
}


@dataclass
class Config:
    sim: SimCfg = field(default_factory=SimCfg)
    dds: DdsCfg = field(default_factory=DdsCfg)
    camera: CameraCfg = field(default_factory=CameraCfg)
    yolo: YoloCfg = field(default_factory=YoloCfg)
    qwen: QwenCfg = field(default_factory=QwenCfg)
    robot: RobotCfg = field(default_factory=RobotCfg)
    ui: UiCfg = field(default_factory=UiCfg)
    path: Path | None = field(default=None, compare=False, repr=False)

    # ------------------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        env = os.environ.get("G1_STUDIO_CONFIG")
        if env:
            return Path(env).expanduser()
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "g1-studio" / "config.toml"

    @classmethod
    def load(cls, path: str | Path | None = None, create: bool = True) -> Config:
        path = Path(path).expanduser() if path else cls.default_path()
        cfg = cls(path=path)
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as e:
                log.warning("Не прочитал %s (%s) — беру настройки по умолчанию", path, e)
                data = {}
            cfg._apply(data)
        elif create:
            try:
                cfg.save()
                log.info("Создал файл настроек %s", path)
            except OSError as e:
                log.warning("Не смог создать %s: %s", path, e)
        return cfg

    def _apply(self, data: dict) -> None:
        for name, value in data.items():
            if name not in SECTIONS or not isinstance(value, dict):
                log.warning("config: неизвестный раздел [%s] — пропускаю", name)
                continue
            section = getattr(self, name)
            known = {f.name: f for f in fields(section)}
            for key, raw in value.items():
                f = known.get(key)
                if f is None:
                    log.warning("config: неизвестное поле %s.%s — пропускаю", name, key)
                    continue
                try:
                    setattr(section, key, coerce(f, raw))
                except ValueError as e:
                    log.warning("config: %s.%s = %r: %s — оставляю %r", name, key, raw, e, getattr(section, key))

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path).expanduser() if path else (self.path or self.default_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_toml(), encoding="utf-8")
        os.replace(tmp, path)                  # атомарно: недописанный файл не останется
        self.path = path
        return path

    def to_toml(self) -> str:
        out = ["# G1 Studio — настройки. Можно править руками или в окне «Настройки».", ""]
        for name, (title, _cls) in SECTIONS.items():
            out.append(f"# --- {title} ---")
            out.append(f"[{name}]")
            section = getattr(self, name)
            for f in fields(section):
                m = f.metadata
                comment = m["label"] + (f". {m['help']}" if m["help"] else "")
                if m["choices"]:
                    comment += f" [{' | '.join(m['choices'])}]"
                out.append(f"# {comment}")
                out.append(f"{f.name} = {toml_value(getattr(section, f.name))}")
            out.append("")
        return "\n".join(out)

    def copy(self) -> Config:
        new = Config(path=self.path)
        for name in SECTIONS:
            src, dst = getattr(self, name), getattr(new, name)
            for f in fields(src):
                setattr(dst, f.name, getattr(src, f.name))
        return new

    def diff(self, other: Config) -> list[tuple[str, str]]:
        """Изменённые поля: [(раздел, поле)]."""
        changed = []
        for name in SECTIONS:
            a, b = getattr(self, name), getattr(other, name)
            changed += [(name, f.name) for f in fields(a) if getattr(a, f.name) != getattr(b, f.name)]
        return changed

    # --- удобства ---
    @property
    def sim_repo(self) -> Path:
        return Path(self.sim.repo).expanduser()

    def api_key(self) -> str:
        return self.qwen.api_key or os.environ.get("QWEN_API_KEY", "")


def restart_needed(changed: list[tuple[str, str]]) -> set[str]:
    out = set()
    for section, key in changed:
        cls = SECTIONS[section][1]
        meta = {f.name: f.metadata for f in fields(cls)}[key]
        if meta["restart"]:
            out.add(meta["restart"])
    return out


def coerce(f, raw):
    """Приводит значение из TOML к типу поля и проверяет пределы/варианты."""
    default = f.default
    m = f.metadata
    if isinstance(default, bool):
        if not isinstance(raw, bool):
            raise ValueError("нужно true или false")
        v = raw
    elif isinstance(default, int):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
            raise ValueError("нужно целое число")
        v = int(raw)
    elif isinstance(default, float):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("нужно число")
        v = float(raw)
    else:
        if not isinstance(raw, str):
            raise ValueError("нужна строка")
        v = raw
    if m["choices"] and v not in m["choices"]:
        raise ValueError(f"допустимо: {', '.join(m['choices'])}")
    if m["min"] is not None and v < m["min"]:
        raise ValueError(f"меньше {m['min']}")
    if m["max"] is not None and v > m["max"]:
        raise ValueError(f"больше {m['max']}")
    return v


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'
