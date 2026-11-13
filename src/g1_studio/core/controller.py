"""Исполнение планов и ручное управление.

Поток отправки шлёт команду в сим 100 раз в секунду (сим сам сбрасывает команду после чтения),
с плавным разгоном/торможением. Поверх него — исполнитель плана (шаги Qwen) и ручное управление.
Любое новое действие прерывает текущее: новая команда, кнопка, «стоп».

События (on_event(kind, data)) — для интерфейса:
  plan        {"steps": [текст шага, ...]}
  step        {"i": номер с 1, "n": всего, "text": ...}
  info / warn {"text": ...}
  done        {"ok": bool, "cancelled": bool}
  sim_speed_hint {"value": оценка скорости сима}
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from g1_studio.core.planner import H_DEFAULT, H_MAX, H_MIN, VX_BACK_MAX, VX_FWD_MAX, VY_MAX, WZ_MAX_DEG, Step
from g1_studio.names import name_ru as ru

log = logging.getLogger("g1studio.robot")


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Controller:
    RATE_HZ = 100
    # Плавность: максимальное изменение команды в секунду. Как у клавиатуры Unitree,
    # тормозим резче, чем разгоняемся (там сброс к нулю вдвое быстрее нажатия).
    ACCEL = (1.5, 1.5, 3.0, 0.5)   # разгон:     vx м/с², vy м/с², wz рад/с², высота м/с
    DECEL = (4.0, 4.0, 6.0, 0.5)   # торможение: то же
    TURN_LEAD_S = 0.25             # робот докручивается после команды «стоп» — упреждение
    TURN_TOL_DEG = 3.0             # точность поворота
    TURN_FIX_DEG = 5.0             # если промах больше — один доворот

    def __init__(self, link, camera=None, sim_speed: float = 1.0,
                 on_event: Callable[[str, dict], None] | None = None):
        self.link = link
        self.camera = camera            # vision.CameraStream или None
        # Скорость сима относительно реального времени. Ходьба «по времени» считает в секундах
        # настенных часов: если Isaac Sim идёт в 0.6×, робот без поправки пройдёт 60% расстояния.
        self.sim_speed = sim_speed
        self.on_event = on_event
        self._rtf_samples: list[float] = []
        self._rtf_warned = False
        self._last_bearing = 0.0        # где цель видели в последний раз (+ слева) — туда и ищем
        self.target = [0.0, 0.0, 0.0, H_DEFAULT]
        self.cmd = [0.0, 0.0, 0.0, H_DEFAULT]
        self._lock = threading.Lock()
        self._alive = True
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._warned_no_imu = False
        self.current_step = ""
        self.mode = "idle"              # idle | plan | manual
        self._pub_thread = threading.Thread(target=self._publish_loop, name="cmd-100hz", daemon=True)
        self._pub_thread.start()

    # --- события ---
    def _emit(self, kind: str, **data) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, data)
            except Exception:
                log.exception("ошибка в обработчике события %s", kind)

    def _info(self, text: str) -> None:
        log.info(text)
        self._emit("info", text=text)

    def _warn(self, text: str) -> None:
        log.warning(text)
        self._emit("warn", text=text)

    # --- поток отправки команд 100 Гц ---
    def _publish_loop(self) -> None:
        dt = 1.0 / self.RATE_HZ
        while self._alive:
            with self._lock:
                tgt = list(self.target)
                for i in range(4):
                    slowing = abs(tgt[i]) < abs(self.cmd[i]) or tgt[i] * self.cmd[i] < 0
                    step = (self.DECEL if slowing else self.ACCEL)[i] * dt
                    self.cmd[i] += max(-step, min(step, tgt[i] - self.cmd[i]))
                cmd = list(self.cmd)
            try:
                self.link.send(*cmd)
            except Exception as e:  # DDS не должен ронять поток: без него робот встанет
                log.error("не отправилась команда: %s", e)
                time.sleep(0.5)
            time.sleep(dt)

    def _set_vel(self, vx=0.0, vy=0.0, wz=0.0) -> None:
        with self._lock:
            self.target[0], self.target[1], self.target[2] = vx, vy, wz

    def _set_height(self, h: float) -> None:
        with self._lock:
            self.target[3] = h

    # --- управление планами ---
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def stop(self) -> None:
        """Прервать план/ручное движение и плавно остановиться."""
        self._cancel.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        self._set_vel(0, 0, 0)
        self.mode = "idle"

    def emergency_stop(self) -> None:
        """Аварийный стоп: прервать всё и сразу обнулить скорость (без плавного торможения)."""
        self.stop()
        with self._lock:
            self.cmd[0] = self.cmd[1] = self.cmd[2] = 0.0
        self._warn("■ аварийная остановка")

    def run(self, steps: list[Step]) -> None:
        """Выполнить план в фоне. Новый план прерывает текущий."""
        self.stop()
        self._cancel = threading.Event()
        self.mode = "plan"
        self._worker = threading.Thread(target=self._execute, args=(steps, self._cancel), name="plan", daemon=True)
        self._worker.start()

    def manual(self, vx: float = 0.0, vy: float = 0.0, wz_deg: float = 0.0) -> None:
        """Ручное управление: держать скорость, пока не придёт другая (кнопки/клавиши)."""
        if self.busy():
            self.stop()
        vx = min(max(vx, -VX_BACK_MAX), VX_FWD_MAX)
        vy = min(max(vy, -VY_MAX), VY_MAX)
        wz = math.radians(min(max(wz_deg, -WZ_MAX_DEG), WZ_MAX_DEG))
        self.mode = "manual" if (vx or vy or wz) else "idle"
        self._set_vel(vx, vy, wz)

    def set_height(self, h: float) -> None:
        self._set_height(min(max(h, H_MIN), H_MAX))

    def shutdown(self) -> None:
        self.stop()
        time.sleep(0.6)          # дать поток нулевых команд, чтобы робот плавно встал
        self._alive = False
        self._pub_thread.join(timeout=1)

    # --- состояние для интерфейса ---
    def sim_speed_estimate(self) -> float | None:
        s = sorted(self._rtf_samples[-60:])
        return s[len(s) // 2] if len(s) >= 10 else None

    def status(self) -> dict:
        with self._lock:
            cmd, tgt = list(self.cmd), list(self.target)
        return {"mode": "plan" if self.busy() else self.mode, "step": self.current_step if self.busy() else "",
                "cmd": cmd, "target": tgt, "sim_speed_est": self.sim_speed_estimate()}

    # --- шаги ---
    def _execute(self, steps: list[Step], cancel: threading.Event) -> None:
        self._emit("plan", steps=[s.describe() for s in steps])
        ok = True
        try:
            for i, s in enumerate(steps, 1):
                if cancel.is_set():
                    ok = False
                    break
                self.current_step = f"[{i}/{len(steps)}] {s.describe()}"
                log.info(self.current_step)
                self._emit("step", i=i, n=len(steps), text=s.describe())
                ok = getattr(self, f"_do_{s.action}")(s, cancel)
                self._set_vel(0, 0, 0)
                if not ok or cancel.wait(0.4):     # короткая пауза между шагами, чтобы робот успокоился
                    ok = False
                    break
        except Exception as e:
            log.exception("ошибка при выполнении плана")
            self._warn(f"ошибка: {e}")
            ok = False
        finally:
            self._set_vel(0, 0, 0)
            self.current_step = ""
            if self.mode == "plan":
                self.mode = "idle"
        if ok:
            log.info("✓ готово")
        self._emit("done", ok=ok, cancelled=cancel.is_set())


    def _move_time(self, dist: float, v: float, axis: int) -> float:
        # Разгон «съедает» v²/2a пути, торможение добавляет v²/2b — поправка по времени.
        # Делим на скорость сима: в медленном симе за секунду проходит меньше симуляционного времени.
        return (abs(dist) / v + v / (2 * self.ACCEL[axis]) - v / (2 * self.DECEL[axis])) / self.sim_speed

    def _do_walk(self, s: Step, cancel) -> bool:
        self._set_vel(vx=math.copysign(s.speed, s.value))
        return not cancel.wait(self._move_time(s.value, s.speed, 0))

    def _do_strafe(self, s: Step, cancel) -> bool:
        self._set_vel(vy=math.copysign(s.speed, s.value))
        return not cancel.wait(self._move_time(s.value, s.speed, 1))

    def _do_turn(self, s: Step, cancel) -> bool:
        goal = math.radians(s.value)
        wz_max = math.radians(s.speed)
        prev = self.link.yaw()
        if prev is None:
            if not self._warned_no_imu:
                self._warn("нет данных IMU из rt/lowstate — поворачиваю по времени (неточно)")
                self._warned_no_imu = True
            self._set_vel(wz=math.copysign(wz_max, goal))
            return not cancel.wait(abs(goal) / wz_max)

        state = {"prev": prev, "turned": 0.0, "omega": 0.0, "t": time.monotonic(), "t0": time.monotonic()}
        if not self._turn_pass(goal, wz_max, state, cancel):
            return False
        self._check_sim_speed()
        # Доворот, если после остановки промах заметный (один раз, медленно)
        err = goal - state["turned"]
        if abs(err) > math.radians(self.TURN_FIX_DEG) and not cancel.is_set():
            if not self._turn_pass(goal, math.radians(20), state, cancel):
                return False
        self._info(f"повернул на {math.degrees(state['turned']):.0f}° (цель {s.value:.0f}°)")
        return True

    def _check_sim_speed(self) -> None:
        s = sorted(self._rtf_samples[-60:])
        if len(s) < 10 or self._rtf_warned:
            return
        rtf = s[len(s) // 2]                         # повороты идут по IMU, от --sim-speed не зависят
        if rtf < 0.75 and abs(rtf - self.sim_speed) > 0.15:   # молчим, если поправка уже задана верно
            self._warn(f"Похоже, сим идёт медленнее реального времени (~{rtf:.2f}×): шаги «на N метров» "
                       f"будут короче. Поставь «Скорость сима» = {rtf:.2f} (Настройки → Робот)")
            self._emit("sim_speed_hint", value=round(rtf, 2))
            self._rtf_warned = True

    def _track_yaw(self, state: dict) -> bool:
        """Обновляет накопленный угол и скорость вращения по IMU. False — данных нет."""
        y = self.link.yaw()
        if y is None:
            return False
        now = time.monotonic()
        d = _wrap(y - state["prev"])           # копим угол — работает и для поворотов больше 180°
        state["turned"] += d
        if now > state["t"]:
            state["omega"] = 0.7 * state["omega"] + 0.3 * d / (now - state["t"])
        state["prev"], state["t"] = y, now
        return True

    def _turn_pass(self, goal: float, wz_max: float, state: dict, cancel) -> bool:
        deadline = time.monotonic() + abs(goal - state["turned"]) / wz_max * 2 + 3
        while not cancel.is_set():
            if not self._track_yaw(state):
                self._warn("пропали данные IMU — останавливаю поворот")
                break
            err = goal - state["turned"]
            # Останавливаемся заранее: после «стоп» робот докрутит ещё ~omega * TURN_LEAD_S
            coast = abs(state["omega"]) * self.TURN_LEAD_S
            if abs(err) < max(math.radians(self.TURN_TOL_DEG), coast) or err * goal < 0:
                break
            if time.monotonic() > deadline:
                self._warn(f"не довернул: осталось {math.degrees(err):.0f}°")
                break
            wz = max(-wz_max, min(wz_max, 2.0 * err))          # к цели замедляемся
            if abs(wz) < 0.25:
                wz = math.copysign(0.25, err)
            self._set_vel(wz=wz)
            # Сколько робот реально крутится от заданного (на ровном участке поворота) —
            # это и есть скорость сима относительно реального времени (× точность походки)
            if (time.monotonic() - state["t0"] > 0.8 and abs(self.cmd[2]) > 0.9 * wz_max
                    and abs(wz) >= 0.9 * wz_max):
                self._rtf_samples.append(state["omega"] / self.cmd[2])
            time.sleep(0.02)
        self._set_vel(0, 0, 0)
        # Ждём, пока робот остановится, и продолжаем мерить — итоговый угол честный
        settle_end = time.monotonic() + 0.8
        while time.monotonic() < settle_end and not cancel.is_set():
            self._track_yaw(state)
            time.sleep(0.02)
        return not cancel.is_set()

    def _do_height(self, s: Step, cancel) -> bool:
        self._set_height(s.value)
        return not cancel.wait(1.5)

    def _do_wait(self, s: Step, cancel) -> bool:
        return not cancel.wait(s.value)

    def _do_stop(self, s: Step, cancel) -> bool:
        self._set_vel(0, 0, 0)
        return True

    # ------------------------------------------------------------------
    # Камера: найти, подойти, толкнуть, посмотреть
    # ------------------------------------------------------------------
    def _vision_ok(self, target: str = "ball") -> bool:
        if self.camera is None:
            self._warn("камера выключена (Настройки → Камера)")
            return False
        if not self.camera.alive():
            self._warn(f"нет кадров с камеры ({self.camera.address}) — сим запущен с --enable_cameras?")
            return False
        if target not in ("", "ball") and self.camera.yolo is None:
            self._warn("YOLO выключен — узнаю только мяч (включи YOLO под картинкой камеры)")
            return False
        return True

    def _get(self, target: str):
        """Текущая детекция цели: мяч — с последнего кадра, объекты YOLO — с последнего обработанного."""
        return self.camera.find(target, 0.3 if target == "ball" else 0.8)

    def _fresh(self, target: str = "ball", timeout: float = 0.6):
        """Детекция по кадру, пришедшему ПОСЛЕ этого момента (а не по старому)."""
        t0 = time.monotonic()
        yolo = target != "ball"
        self.camera.wait_frame(t0, max(timeout, 1.2) if yolo else timeout, yolo=yolo)
        det = self.camera.find(target, 1.2 if yolo else 0.5)
        if det is not None:
            self._last_bearing = det.bearing_deg
        return det

    def _settle(self, seconds: float, cancel) -> bool:
        self._set_vel(0, 0, 0)
        return not cancel.wait(seconds)

    def _do_look(self, s: Step, cancel) -> bool:
        if not self._vision_ok(s.target or "ball"):
            return True
        if s.target:
            det = self._fresh(s.target)
            self._info(f"👁 {det.describe()}" if det else f"👁 не вижу: {ru(s.target)}")
            return True
        self.camera.wait_frame(time.monotonic(), 1.2, yolo=self.camera.yolo is not None)
        items = self.camera.visible()
        self._info("👁 вижу: " + "; ".join(o.describe() for o in items) if items else "👁 ничего знакомого не вижу")
        return True

    def _do_find(self, s: Step | None, cancel, target: str | None = None) -> bool:
        """Поворот на месте (в сторону, где цель видели в последний раз), пока она не окажется в кадре."""
        target = target or (s.target if s else "") or "ball"
        if not self._vision_ok(target):
            return False
        det = self._fresh(target)
        if det is not None:
            self._info(f"👁 вижу: {det.describe()}")
            return True
        speed = 0.6 if target == "ball" else 0.4          # YOLO реже обновляется — крутимся медленнее
        wz = math.copysign(speed, self._last_bearing if self._last_bearing else 1.0)
        prev, turned = self.link.yaw(), 0.0
        deadline = time.monotonic() + 2 * math.pi / abs(wz) + 4
        self._set_vel(wz=wz)
        while not cancel.is_set() and time.monotonic() < deadline:
            det = self._get(target)
            if det is not None and abs(det.bearing_deg) < 40:
                if not self._settle(0.6, cancel):
                    return False
                det = self._fresh(target) or det
                self._info(f"👁 нашёл: {det.describe()}")
                return True
            y = self.link.yaw()
            if y is not None and prev is not None:
                turned += _wrap(y - prev)
                if abs(turned) > 2 * math.pi:
                    break
            prev = y
            time.sleep(0.03)
        self._set_vel(0, 0, 0)
        if not cancel.is_set():
            self._info(f"{ru(target)}: не нашёл — обернулся вокруг себя")
        return False

    def _approach(self, target: str, stop_dist: float, cancel, v_max: float = 0.45) -> bool:
        """Визуальное наведение: на каждом кадре поворот на цель и шаг вперёд, пока не встанем на stop_dist."""
        if not self._vision_ok(target):
            return False
        if self._fresh(target) is None and not self._do_find(None, cancel, target):
            return False
        is_ball = target == "ball"
        lost_after = 1.5 if is_ball else 2.5               # YOLO иногда «моргает» — даём ему время
        deadline = time.monotonic() + 45
        lost_since, last, refinds = None, None, 0
        while not cancel.is_set():
            now = time.monotonic()
            det = self._get(target)
            if det is None:
                lost_since = lost_since or now
                if now - lost_since > 0.3:
                    self._set_vel(0, 0, 0)
                if now - lost_since > lost_after:
                    if last is not None and last.distance_m < stop_dist + 0.35:
                        self._info(f"{ru(target)}: ушёл из кадра у самых ног — стою")
                        break
                    if refinds >= 2:
                        self._info(f"потерял: {ru(target)}")
                        return False
                    self._info(f"потерял: {ru(target)} — ищу")
                    refinds += 1
                    if not self._do_find(None, cancel, target):
                        return False
                    lost_since = None
                time.sleep(0.03)
                continue
            lost_since, last = None, det
            self._last_bearing = det.bearing_deg
            err = det.distance_m - stop_dist
            if abs(err) < 0.06 and abs(det.bearing_deg) < 8:
                break
            if not is_ball and det.clipped and abs(det.bearing_deg) < 15:
                self._info(f"{ru(target)}: низ ушёл за кадр — значит, уже рядом, стою")
                break
            if now > deadline:
                self._warn("не дошёл за 45 с")
                break
            wz = max(-0.9, min(0.9, 1.8 * math.radians(det.bearing_deg)))
            if abs(det.bearing_deg) > 25:
                vx = 0.0                                   # сначала довернуть, потом идти
            else:
                vx = max(-0.2, min(v_max, 0.8 * err))
                if 0 < vx < 0.12:
                    vx = 0.12                              # иначе походка «топчется» на месте
            self._set_vel(vx=vx, wz=wz)
            time.sleep(0.03)
        if not self._settle(0.6, cancel):
            return False
        det = self._fresh(target)
        self._info(f"стою: {det.describe()}" if det else f"стою: {ru(target)} у самых ног")
        return True

    def _do_approach(self, s: Step, cancel) -> bool:
        return self._approach(s.target or "ball", s.value, cancel)

    def _do_push(self, s: Step, cancel) -> bool:
        """Подойти на 0.45 м, выровняться и пройти сквозь мяч на нужное расстояние."""
        if not self._approach("ball", 0.45, cancel):
            return False
        contact = 0.45 - 0.25                              # центр мяча ~0.25 м от торса = касание ногами
        speed = 0.3
        end = time.monotonic() + ((contact + s.value) / speed + speed / (2 * self.ACCEL[0])) / self.sim_speed
        while not cancel.is_set() and time.monotonic() < end:
            det = self.camera.latest(0.3)
            wz = max(-0.5, min(0.5, 1.5 * math.radians(det.bearing_deg))) if det else 0.0
            self._set_vel(vx=speed, wz=wz)                 # держим мяч по центру, пока его видно
            time.sleep(0.03)
        if not self._settle(1.5, cancel):                  # дать мячу докатиться
            return False
        det = self._fresh("ball")
        self._info(f"👁 после толчка: {det.describe()}" if det else "👁 мяч укатился из кадра")
        return True
