"""Правка сцены задачи Isaac-Move-Cylinder-G129-Dex1-Wholebody: красный мяч и предметы для YOLO.

Патч вставляет в конфиг сцены помеченные блоки (# >>> G1_BALL / # >>> G1_OBJECTS), которые
читают переменные окружения при старте сима — поэтому включать/выключать мяч и предметы
можно без повторной правки: G1_BALL=0, G1_BALL_POS="x,y", G1_OBJECTS=0, G1_OBJECTS_FILE=....
G1 Studio сама выставляет их при запуске сима по настройкам.

Перед первой правкой файл задачи копируется в *.bak / *.bak_objects.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

TASK_FILE = "tasks/g1_tasks/move_cylinder_g1_29dof_dex1_wholebody/move_cylinder_g1_29dof_dex1_hw_env_cfg.py"
OBJECTS_JSON = "scene_objects.json"
MAX_OBJECTS = 8

BALL = ("# >>> G1_BALL", "# <<< G1_BALL", "# G1_BALL")
OBJECTS = ("# >>> G1_OBJECTS", "# <<< G1_OBJECTS", "# G1_OBJECTS")


class SceneError(RuntimeError):
    pass


BALL_HELPER = f'''{BALL[0]} (добавлено G1 Studio; убрать: g1-studio scene ball off)
import os as _g1_os
import isaaclab.sim as _g1_sim
from isaaclab.assets import RigidObjectCfg as _G1RigidObjectCfg


def _g1_ball_cfg():
    """Красный мяч на полу. G1_BALL=0 — выключить, G1_BALL_POS="x,y" — переставить."""
    if _g1_os.environ.get("G1_BALL", "1") == "0":
        return None
    try:
        x, y = (float(v) for v in _g1_os.environ.get("G1_BALL_POS", "-2.7,-2.0").split(","))
    except ValueError:
        x, y = -2.7, -2.0
    r = 0.1
    return _G1RigidObjectCfg(
        prim_path="/World/envs/env_.*/Ball",
        init_state=_G1RigidObjectCfg.InitialStateCfg(pos=(x, y, r + 0.005), rot=(1, 0, 0, 0)),
        spawn=_g1_sim.SphereCfg(
            radius=r,
            # демпфирование: без него в PhysX мяч катится почти бесконечно
            rigid_props=_g1_sim.RigidBodyPropertiesCfg(linear_damping=0.3, angular_damping=0.5),
            mass_props=_g1_sim.MassPropertiesCfg(mass=0.4),
            collision_props=_g1_sim.CollisionPropertiesCfg(),
            visual_material=_g1_sim.PreviewSurfaceCfg(diffuse_color=(0.9, 0.05, 0.05), roughness=0.4),
            physics_material=_g1_sim.RigidBodyMaterialCfg(
                static_friction=0.8, dynamic_friction=0.6, restitution=0.3),
        ),
    )
{BALL[1]}


'''


def objects_helper(json_path: Path) -> str:
    return f'''{OBJECTS[0]} (добавлено G1 Studio; убрать: g1-studio scene objects off)
import json as _g1o_json
import os as _g1o_os
import isaaclab.sim as _g1o_sim
from isaaclab.assets import AssetBaseCfg as _G1AssetBaseCfg

_G1O_FILE = _g1o_os.environ.get("G1_OBJECTS_FILE", {str(json_path)!r})


def _g1_obj(i):
    """i-й предмет из scene_objects.json или None (нет файла / предметов меньше / G1_OBJECTS=0)."""
    if _g1o_os.environ.get("G1_OBJECTS", "1") == "0":
        return None
    try:
        with open(_G1O_FILE, encoding="utf-8") as f:
            objs = _g1o_json.load(f)["objects"]
        o = objs[i]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    s = float(o.get("scale", 1.0))
    return _G1AssetBaseCfg(
        prim_path="{{ENV_REGEX_NS}}/G1Obj_%d" % i,
        spawn=_g1o_sim.UsdFileCfg(usd_path=o["usd"], scale=(s, s, s)),
        init_state=_G1AssetBaseCfg.InitialStateCfg(pos=tuple(o["pos"]), rot=tuple(o.get("rot", (1, 0, 0, 0)))),
    )
{OBJECTS[1]}


'''


# ----------------------------------------------------------------------------
def task_path(repo: Path) -> Path:
    p = Path(repo).expanduser() / TASK_FILE
    if not p.is_file():
        raise SceneError(f"нет файла задачи {p} — проверь папку unitree_sim_isaaclab в настройках")
    return p


def _remove(text: str, marks: tuple[str, str, str]) -> str:
    begin, end, tag = marks
    text = re.sub(rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n*", "\n", text, flags=re.S)
    return "\n".join(line for line in text.split("\n") if not line.rstrip().endswith(tag))


def _insert(text: str, helper: str, fields: str) -> str:
    cls = re.search(r"^@configclass\s*\nclass ObjectTableSceneCfg\(", text, flags=re.M)
    if not cls:
        raise SceneError("не нашёл class ObjectTableSceneCfg — файл задачи изменился (обнови G1 Studio)")
    anchor = re.search(r"^(\s+)robot_camera\s*=.*$", text[cls.end():], flags=re.M)
    if not anchor:
        raise SceneError("не нашёл строку robot_camera = ... в ObjectTableSceneCfg — файл задачи изменился")
    indent = anchor.group(1)
    pos = cls.end() + anchor.end()
    text = text[:pos] + fields.replace("{indent}", indent) + text[pos:]
    return text[:cls.start()] + helper + text[cls.start():]


def _write(path: Path, text: str, backup_suffix: str) -> None:
    compile(text, str(path), "exec")                  # файл должен остаться корректным Python
    backup = path.with_suffix(path.suffix + backup_suffix)
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")


def set_ball(repo: Path, on: bool) -> bool:
    """Добавить/убрать мяч. True — файл изменён."""
    path = task_path(repo)
    text = path.read_text(encoding="utf-8")
    clean = _remove(text, BALL) if BALL[0] in text else text
    if not on:
        if clean == text:
            return False
        path.write_text(clean, encoding="utf-8")
        return True
    new = _insert(clean, BALL_HELPER, f"\n{{indent}}ball: _G1RigidObjectCfg | None = _g1_ball_cfg()  {BALL[2]}")
    if new == text:
        return False
    _write(path, new, ".bak")
    return True


def set_objects(repo: Path, on: bool, json_path: Path | None = None) -> bool:
    """Подключить/убрать предметы из scene_objects.json. True — файл изменён."""
    repo = Path(repo).expanduser()
    path = task_path(repo)
    text = path.read_text(encoding="utf-8")
    clean = _remove(text, OBJECTS) if OBJECTS[0] in text else text
    if not on:
        if clean == text:
            return False
        path.write_text(clean, encoding="utf-8")
        return True
    json_path = Path(json_path).resolve() if json_path else repo / OBJECTS_JSON
    fields = "".join(f"\n{{indent}}obj_{i}: _G1AssetBaseCfg | None = _g1_obj({i})  {OBJECTS[2]}"
                     for i in range(MAX_OBJECTS))
    new = _insert(clean, objects_helper(json_path), fields)
    if new == text:
        return False
    _write(path, new, ".bak_objects")
    return True


@dataclass
class SceneStatus:
    task_file: Path | None
    error: str
    ball: bool
    objects: bool
    objects_json: Path | None
    objects_list: list[str]

    def describe(self) -> list[str]:
        if self.error:
            return [f"✗ {self.error}"]
        out = [f"Мяч: {'добавлен' if self.ball else 'нет'}",
               f"Предметы: {'подключены' if self.objects else 'не подключены'}"]
        if self.objects_json is not None:
            if self.objects_list:
                out.append(f"  {self.objects_json.name}: {len(self.objects_list)} шт. — {', '.join(self.objects_list)}")
            else:
                out.append(f"  {self.objects_json.name}: нет файла или пуст — нажми «Найти предметы»")
        return out


def status(repo: Path) -> SceneStatus:
    try:
        path = task_path(repo)
    except SceneError as e:
        return SceneStatus(None, str(e), False, False, None, [])
    text = path.read_text(encoding="utf-8")
    json_path = None
    m = re.search(r'_G1O_FILE = _g1o_os\.environ\.get\("G1_OBJECTS_FILE", (.+?)\)\n', text)
    if m:
        try:
            json_path = Path(ast.literal_eval(m.group(1)))     # строковый литерал, который мы же записали
        except Exception:
            json_path = None
    json_path = json_path or Path(repo).expanduser() / OBJECTS_JSON
    labels = []
    try:
        labels = [o.get("label", "?") for o in json.loads(json_path.read_text(encoding="utf-8"))["objects"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return SceneStatus(path, "", BALL[0] in text, OBJECTS[0] in text, json_path, labels)


def find_objects_script() -> Path:
    """Скрипт поиска предметов в библиотеке Isaac Sim (запускается в окружении сима)."""
    return Path(__file__).resolve().parents[1] / "isaac" / "find_scene_objects.py"
