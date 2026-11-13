"""Геометрия головной камеры G1: пиксель -> направление и расстояние по полу.

Камера (из URDF Unitree G1 и CameraBaseCfg сима):
  * D435 на голове: смещение от torso_link (0.0576, 0.0175, 0.42) м, наклон вниз 47.6°;
  * 640×480, фокус 7.6 мм, апертура 20 мм -> fx = fy = 243.2 px (угол обзора 105°×89°).
"""
from __future__ import annotations

import math

import numpy as np

# --- камера (как в симе Unitree) ---
IMG_W, IMG_H = 640, 480
FX = FY = 7.6 / 20.0 * IMG_W            # 243.2 px
CX, CY = IMG_W / 2.0, IMG_H / 2.0
CAM_PITCH = 0.8307767                    # рад, наклон вниз (47.6°), из g1_29dof.urdf
CAM_OFFSET = (0.0576235, 0.01753, 0.41987)  # м, относительно torso_link
TORSO_HEIGHT = 0.83                      # м над полом, стоя (таз ~0.78 + 0.054 до торса)

BALL_RADIUS = 0.1                        # м — как в add_ball_to_scene.py


# ----------------------------------------------------------------------------
# Геометрия: пиксель -> луч в системе торса
# ----------------------------------------------------------------------------
def pixel_ray_torso(u: float, v: float) -> np.ndarray:
    """Единичный луч из камеры через пиксель (u, v) в системе torso_link (x вперёд, y влево, z вверх)."""
    xo, yo, zo = (u - CX) / FX, (v - CY) / FY, 1.0         # оптическая система: x вправо, y вниз, z вперёд
    lx, ly, lz = zo, -xo, -yo                             # -> d435_link: x вперёд, y влево, z вверх
    c, s = math.cos(CAM_PITCH), math.sin(CAM_PITCH)
    ray = np.array([c * lx + s * lz, ly, -s * lx + c * lz])  # поворот вокруг y на наклон камеры
    return ray / np.linalg.norm(ray)


def ground_locate(u: float, v: float, z: float = 0.0) -> tuple[float, float]:
    """(направление°, горизонтальная дистанция м) точки пола (на высоте z), видимой в пикселе (u, v)."""
    ray = pixel_ray_torso(u, v)
    cam = np.array(CAM_OFFSET)
    cam_z = TORSO_HEIGHT + cam[2]
    if ray[2] >= -1e-3:                                   # луч выше горизонта — пол не пересекает
        return math.degrees(math.atan2(ray[1], ray[0])), float("inf")
    p = cam + ray * ((cam_z - z) / -ray[2])
    return math.degrees(math.atan2(p[1], p[0])), math.hypot(p[0], p[1])


def locate(u: float, v: float, r_px: float, clipped: bool) -> tuple[float, float, float]:
    """(направление°, горизонтальная дистанция м, высота центра над полом м) для мяча."""
    ray = pixel_ray_torso(u, v)
    cam = np.array(CAM_OFFSET)
    if not clipped and r_px > 1.5:
        # Видимый размер. Для сферы под углом θ к оптической оси поперечная полуось ≈ f·tanα / cosθ,
        # где sinα = R/D. Отсюда D = R·sqrt(1 + (f/(r·cosθ))²).
        cos_t = 1.0 / math.sqrt(1 + ((u - CX) / FX) ** 2 + ((v - CY) / FY) ** 2)
        k = FX / (r_px * cos_t)
        dist3d = BALL_RADIUS * math.sqrt(1 + k * k)
        p = cam + ray * dist3d
    else:
        # Мяч обрезан: пересекаем луч с плоскостью z = R (центр мяча) при известной высоте торса
        cam_z = TORSO_HEIGHT + cam[2]
        if ray[2] >= -1e-3:
            return 0.0, float("inf"), float("nan")
        t = (cam_z - BALL_RADIUS) / -ray[2]
        p = cam + ray * t
    bearing = math.degrees(math.atan2(p[1], p[0]))
    dist = math.hypot(p[0], p[1])
    height = TORSO_HEIGHT + p[2]                 # центр мяча над полом при обычной высоте торса
    return bearing, dist, height


def project_torso(p) -> tuple[float, float] | None:
    """Обратная задача: точка в системе torso_link -> пиксель (u, v); None, если за камерой."""
    d = np.asarray(p, float) - np.array(CAM_OFFSET)
    c, s = math.cos(CAM_PITCH), math.sin(CAM_PITCH)
    lx, ly, lz = c * d[0] - s * d[2], d[1], s * d[0] + c * d[2]   # обратный поворот вокруг y
    if lx <= 1e-6:
        return None
    xo, yo = -ly / lx, -lz / lx
    return CX + FX * xo, CY + FY * yo
