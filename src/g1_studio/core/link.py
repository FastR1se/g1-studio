"""Связь с симом по DDS — тем же способом, что send_commands_keyboard.py от Unitree.

Команда: DDS domain 1, топик rt/run_command/cmd, строка "[vx, vy, wz, height]".
Сим сбрасывает команду в [0, 0, 0, 0.8] после каждого чтения, поэтому её надо слать
постоянно (это делает Controller, 100 Гц). Упало приложение — робот встал.
Ориентация: rt/lowstate (LowState_ unitree_hg), imu_state.quaternion.
"""
from __future__ import annotations

import logging
import math
import threading
import time

log = logging.getLogger("g1studio.dds")

_factory_lock = threading.Lock()
_factory_domain: int | None = None


class LinkUnavailable(RuntimeError):
    pass


def yaw_from_quat(q, order: str) -> float:
    if order == "xyzw":
        x, y, z, w = q
    else:
        w, x, y, z = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def dds_available() -> tuple[bool, str]:
    try:
        import unitree_sdk2py.core.channel  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, f"нет unitree_sdk2py/cyclonedds ({e}) — поставь их: bash scripts/setup_studio.sh"
    except Exception as e:  # cyclonedds без libddsc и т.п.
        return False, f"DDS не загрузился: {e}"


class SimLink:
    """DDS: команды в rt/run_command/cmd, ориентация из rt/lowstate."""

    def __init__(self, domain: int = 1, interface: str | None = None, quat_order: str = "xyzw"):
        global _factory_domain
        ok, why = dds_available()
        if not ok:
            raise LinkUnavailable(why)
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

        with _factory_lock:                        # DDS-фабрику можно поднять один раз на процесс
            if _factory_domain is None:
                if interface:
                    ChannelFactoryInitialize(domain, interface)
                else:
                    ChannelFactoryInitialize(domain)
                _factory_domain = domain
            elif _factory_domain != domain:
                log.warning("DDS уже поднят на domain %s — смена domain вступит после перезапуска приложения",
                            _factory_domain)
        self.domain = _factory_domain
        self._String = String_
        self.quat_order = quat_order
        self._yaw: float | None = None
        self._t_state = 0.0
        self._n_state = 0
        self._rate = 0.0
        self._rate_t0 = time.monotonic()
        self._rate_n0 = 0
        self.pub = ChannelPublisher("rt/run_command/cmd", String_)
        self.pub.Init()
        self.sub = ChannelSubscriber("rt/lowstate", LowState_)
        self.sub.Init(self._on_state, 10)

    def _on_state(self, msg) -> None:
        q = list(msg.imu_state.quaternion)
        if any(q):
            self._yaw = yaw_from_quat(q, self.quat_order)
            self._t_state = time.monotonic()
            self._n_state += 1

    # --- состояние ---
    def yaw(self) -> float | None:
        return self._yaw if time.monotonic() - self._t_state < 0.5 else None

    def state_age(self) -> float:
        return time.monotonic() - self._t_state if self._t_state else float("inf")

    def state_rate(self) -> float:
        """Сколько сообщений rt/lowstate в секунду (за последние ~2 с)."""
        now = time.monotonic()
        if now - self._rate_t0 >= 2.0:
            self._rate = (self._n_state - self._rate_n0) / (now - self._rate_t0)
            self._rate_t0, self._rate_n0 = now, self._n_state
        return self._rate if self.state_age() < 1.0 else 0.0

    def send(self, vx: float, vy: float, wz: float, h: float) -> None:
        # Ровно тот формат, что у send_commands_keyboard.py: "[x_vel, y_vel, yaw_vel, height]"
        self.pub.Write(self._String(data=str([round(vx, 3), round(vy, 3), round(wz, 3), round(h, 3)])))

    @property
    def kind(self) -> str:
        return f"DDS domain {self.domain}"


class DryLink:
    """Без сима: ничего не отправляет, ориентации нет. Для проверки планов и интерфейса."""

    domain = None
    kind = "без сима (dry-run)"
    quat_order = "xyzw"

    def __init__(self, reason: str = ""):
        self.reason = reason
        self.last = (0.0, 0.0, 0.0, 0.8)

    def yaw(self):
        return None

    def state_age(self) -> float:
        return float("inf")

    def state_rate(self) -> float:
        return 0.0

    def send(self, *cmd):
        self.last = cmd
