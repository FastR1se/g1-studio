#!/usr/bin/env python3
"""
find_scene_objects.py — находит в библиотеке ассетов Isaac Sim модели предметов, которые знает YOLO,
меряет их и расставляет вокруг робота. Результат — scene_objects.json, его читает сцена
(после «Подключить предметы» в G1 Studio или g1-studio scene objects on).

Зачем так, а не готовый список путей: в разных версиях Isaac Sim модели лежат по-разному,
а неверный путь роняет сим на старте. Скрипт сам проверяет, что модель реально есть
и какого она размера (бывают модели в сантиметрах или «лёжа» — это он исправит).

Запускается в окружении сима (нужен интернет — библиотека ассетов NVIDIA в облаке), ~2–10 минут:
  G1 Studio → вкладка «Сцена» → «Найти предметы»,   или   g1-studio scene find

Что ищет (классы COCO, которые YOLO узнаёт): человек, стул, растение, бутылка, кружка, миска,
банан, чемодан, диван, ваза, ноутбук, рюкзак. Ставит до 8 штук на пол вокруг стартовой
точки робота, не задевая столы, мяч и путь к мячу.

Позиции можно поправить руками в scene_objects.json (pos — x, y, z в метрах в системе сцены;
робот стартует в (-3.9, -2.818) лицом по +x).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--out", default=os.path.join(os.getcwd(), "scene_objects.json"))
ap.add_argument("--max", type=int, default=8, help="сколько предметов поставить (до 8)")
ap.add_argument("--max-folders", type=int, default=500, help="сколько папок библиотеки просмотреть максимум")
args = ap.parse_args()

# --- Isaac Sim без окна ---
from isaaclab.app import AppLauncher  # noqa: E402

app = AppLauncher(headless=True).app

import omni.client  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Usd, UsdGeom  # noqa: E402

import isaaclab.sim as sim_utils  # noqa: E402
from isaaclab.utils.assets import ISAAC_NUCLEUS_DIR, NVIDIA_NUCLEUS_DIR  # noqa: E402

ROBOT_START = (-3.9, -2.818)          # из конфига задачи Move-Cylinder: робот лицом по +x

# класс: ключевые слова в пути, целевой размер (м), мерить по высоте (True) или по наибольшей стороне,
#        место (вперёд, влево) от стартовой точки робота
CLASSES = {
    "person":       (["/people/characters/"],               1.75, True,  (2.6, 1.3)),
    "chair":        (["chair"],                             0.90, True,  (0.2, 2.1)),
    "potted plant": (["pottedplant", "potted_plant", "houseplant", "plant_pot", "plant"], 0.80, True, (1.9, 2.5)),
    "bottle":       (["bottle", "mustard", "bleach"],      0.25, True,  (0.8, -0.6)),
    "cup":          (["mug", "_cup", "/cup", "coffeecup"],   0.12, False, (0.9, 1.5)),
    "bowl":         (["bowl"],                              0.16, False, (0.4, 1.0)),
    "banana":       (["banana"],                            0.20, False, (0.95, 0.05)),
    "suitcase":     (["suitcase", "luggage"],               0.65, True,  (-0.3, 1.7)),
    "couch":        (["sofa", "couch"],                     1.90, False, (3.3, 2.9)),
    "vase":         (["vase"],                              0.30, True,  (2.2, 0.6)),
    "laptop":       (["laptop"],                            0.33, False, (1.5, 1.6)),
    "backpack":     (["backpack"],                          0.45, True,  (2.4, 1.9)),
}
EXCLUDE = ("material", "/mdl", "texture", "anim", "skel", "biped", "collision", "cupboard", "bookshelf",
           "armchair_base", "_lod", "thumbnail", "/.thumbs", "powerplant", "plantation")
USD_EXT = (".usd", ".usda", ".usdc", ".usdz")

ROOTS = [   # (папка, глубина) — по приоритету
    (f"{ISAAC_NUCLEUS_DIR}/People/Characters", 2),
    (f"{ISAAC_NUCLEUS_DIR}/Props/YCB/Axis_Aligned", 1),
    (f"{ISAAC_NUCLEUS_DIR}/Environments/Office/Props", 2),
    (f"{ISAAC_NUCLEUS_DIR}/Environments/Hospital/Props", 2),
    (f"{ISAAC_NUCLEUS_DIR}/Props", 2),
    (f"{NVIDIA_NUCLEUS_DIR}/Assets/ArchVis/Residential", 4),
    (f"{NVIDIA_NUCLEUS_DIR}/Assets/ArchVis/Commercial", 3),
    (f"{NVIDIA_NUCLEUS_DIR}/Assets/Vegetation", 3),
]


def say(msg: str) -> None:
    print(msg, flush=True)


def classify(path: str) -> str | None:
    p = path.lower()
    if not p.endswith(USD_EXT) or any(x in p for x in EXCLUDE):
        return None
    name = p.rsplit("/", 1)[-1]
    if "/people/characters/" in p:
        # персонаж: берём файл, названный как его папка (без служебных)
        parent = p.rsplit("/", 2)[-2]
        return "person" if name.rsplit(".", 1)[0] == parent else None
    for label, (keys, *_rest) in CLASSES.items():
        if label == "person":
            continue
        if any(k in name for k in keys):
            return label
    return None


def walk(root: str, depth: int, budget: list, found: dict, per_class: int = 2) -> None:
    """Обход папки с ограничением глубины и общего числа просмотренных папок."""
    queue = [(root, 0)]
    while queue and budget[0] > 0:
        url, d = queue.pop(0)
        budget[0] -= 1
        try:
            result, entries = omni.client.list(url)
        except Exception:
            continue
        if result != omni.client.Result.OK:
            continue
        for e in entries:
            child = f"{url.rstrip('/')}/{e.relative_path}"
            if e.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN and not child.lower().endswith(USD_EXT):
                if d + 1 <= depth:
                    queue.append((child, d + 1))
                continue
            label = classify(child)
            if label and len(found.setdefault(label, [])) < per_class:
                found[label].append(child)
                say(f"    нашёл {label:13} {child.split('/Assets/', 1)[-1]}")
        if all(len(found.get(c, [])) >= per_class for c in CLASSES):
            return


def measure(stage, url: str, idx: int):
    """Загружает модель так же, как Isaac Lab (UsdFileCfg), и меряет габариты в метрах сцены."""
    prim_path = f"/World/Probe_{idx}"
    cfg = sim_utils.UsdFileCfg(usd_path=url)
    cfg.func(prim_path, cfg)
    for _ in range(5):
        app.update()                                   # дать подгрузиться ссылкам
    prim = stage.GetPrimAtPath(prim_path)
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
    rng = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    stage.RemovePrim(prim_path)
    if rng.IsEmpty():
        return None
    lo, hi = rng.GetMin(), rng.GetMax()
    return [lo[0], lo[1], lo[2]], [hi[0], hi[1], hi[2]]


def rotate_x(p, rot):
    """Поворот точки кватернионом (w, x, 0, 0) — только вокруг оси X (другие нам не нужны)."""
    ang = 2 * math.atan2(rot[1], rot[0])
    c, s = math.cos(ang), math.sin(ang)
    return (p[0], p[1] * c - p[2] * s, p[1] * s + p[2] * c)


def place(label: str, lo, hi) -> dict | None:
    _keys, target, by_height, (fwd, left) = CLASSES[label]
    size = [hi[i] - lo[i] for i in range(3)]
    rot = (1.0, 0.0, 0.0, 0.0)
    note = []
    # модель «лёжа» (Y-вверх): высокий предмет, у которого y заметно больше z — поднимаем на 90° вокруг X
    if by_height and size[1] > 1.4 * size[2] and size[1] >= size[0]:
        rot = (math.cos(math.pi / 4), math.sin(math.pi / 4), 0.0, 0.0)
        note.append("была «лёжа» — поставил вертикально")
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    rc = [rotate_x(c, rot) for c in corners]
    rlo = [min(c[i] for c in rc) for i in range(3)]
    rhi = [max(c[i] for c in rc) for i in range(3)]
    rsize = [rhi[i] - rlo[i] for i in range(3)]
    measured = rsize[2] if by_height else max(rsize)
    if measured <= 1e-4:
        return None
    ratio = target / measured
    scale = 1.0
    if not 0.5 <= ratio <= 2.0:                       # явно не те единицы (см/мм) — приводим к реальному размеру
        scale = ratio
        note.append(f"размер {measured:.3g} вместо ~{target} м — масштаб {scale:.3g}")
    cx, cy = (rlo[0] + rhi[0]) / 2 * scale, (rlo[1] + rhi[1]) / 2 * scale
    x, y = ROBOT_START[0] + fwd, ROBOT_START[1] + left
    return {
        "label": label, "pos": [round(x - cx, 4), round(y - cy, 4), round(-rlo[2] * scale + 0.002, 4)],
        "rot": [round(v, 6) for v in rot], "scale": round(scale, 6),
        "size_m": [round(v * scale, 3) for v in rsize], "note": "; ".join(note),
    }


def main() -> int:
    say(f"Библиотека ассетов: {ISAAC_NUCLEUS_DIR.rsplit('/Isaac', 1)[0]}")
    found: dict[str, list[str]] = {}
    budget = [args.max_folders]
    t0 = time.time()
    for root, depth in ROOTS:
        say(f"  смотрю {root.split('/Assets/', 1)[-1]} ...")
        walk(root, depth, budget, found)
        if budget[0] <= 0:
            say("  ! лимит просмотра папок исчерпан (--max-folders)")
            break
    say(f"Поиск занял {time.time() - t0:.0f} с, папок просмотрено: {args.max_folders - budget[0]}")

    ctx = omni.usd.get_context()
    ctx.new_stage()
    stage = ctx.get_stage()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.DefinePrim("/World", "Xform")

    objects, idx = [], 0
    for label in CLASSES:                          # порядок = приоритет
        if len(objects) >= min(args.max, 8):
            break
        for url in found.get(label, []):
            idx += 1
            try:
                box = measure(stage, url, idx)
            except Exception as e:
                say(f"  ✗ {label}: не загрузилась {url.rsplit('/', 1)[-1]}: {e}")
                continue
            if box is None:
                say(f"  ✗ {label}: пустая модель {url.rsplit('/', 1)[-1]}")
                continue
            o = place(label, *box)
            if o is None:
                continue
            o["usd"] = url
            objects.append(o)
            say(f"  ✓ {label:13} {o['size_m']} м  {o['note']}")
            break

    data = {"generated": time.strftime("%Y-%m-%d %H:%M"), "robot_start": list(ROBOT_START), "objects": objects}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    missing = [c for c in CLASSES if c not in {o["label"] for o in objects}]
    say(f"\n✓ Записал {len(objects)} предметов в {args.out}")
    if missing:
        say(f"  Не нашлось в библиотеке: {', '.join(missing)} (это нормально — ставим то, что есть)")
    say("Дальше: подключи предметы (G1 Studio → «Сцена») и перезапусти сим")
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        sys.stdout.flush()
        app.close()          # закрывает процесс целиком — поэтому всё важное выше уже записано
    sys.exit(rc)
