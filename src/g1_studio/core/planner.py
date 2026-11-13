"""Qwen превращает текст в план движений; план проверяется и обрезается по безопасным пределам.

Бэкенды: локальная Ollama (по умолчанию на CPU — видеопамять нужна Isaac Sim) или любой
OpenAI-совместимый API (например DashScope от Alibaba).
"""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from g1_studio.names import TARGET_RU, TARGETS
from g1_studio.names import name_ru as ru

# ----------------------------------------------------------------------------
# Пределы — те же, что в send_commands_keyboard.py от Unitree
# ----------------------------------------------------------------------------
VX_FWD_MAX, VX_BACK_MAX = 1.0, 0.6     # м/с вперёд / назад
VY_MAX = 0.5                           # м/с вбок
WZ_MAX_DEG = 90.0                      # град/с (1.57 рад/с)
H_MIN, H_MAX, H_DEFAULT = 0.3, 0.8, 0.8  # высота корпуса, м

DEFAULTS = {"walk": 0.5, "strafe": 0.3, "turn": 45.0}  # м/с, м/с, град/с
MIN_SPEED = {"walk": 0.1, "strafe": 0.1, "turn": 10.0}
MAX_DISTANCE_M = 5.0
MAX_ANGLE_DEG = 720.0
MAX_WAIT_S = 30.0
MAX_STEPS = 12

ACTIONS = ["walk", "strafe", "turn", "height", "wait", "stop", "find", "approach", "push", "look"]
VISION_ACTIONS = {"find", "approach", "push", "look"}
APPROACH_DEFAULT, APPROACH_MIN, APPROACH_MAX = 0.5, 0.3, 2.0   # на каком расстоянии от мяча встать, м
APPROACH_OBJECT_DEFAULT = 0.7                                   # от других объектов — подальше

PUSH_DEFAULT, PUSH_MIN, PUSH_MAX = 0.5, 0.2, 1.5               # на сколько протолкнуть мяч, м
STOP_WORDS = {"стоп", "stop", "стой", "хватит", "остановись", "s", "с", "halt"}
EXIT_WORDS = {"q", "выход", "exit", "quit"}

# ----------------------------------------------------------------------------
# Промпт и схема ответа
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — модуль управления человекоподобным роботом Unitree G1. Переводи команду пользователя в план движений. Отвечай ТОЛЬКО JSON.

Действия (поле action) и смысл value:
- "walk": идти вперёд или назад. value — метры: плюс вперёд, минус назад.
- "strafe": идти боком. value — метры: плюс влево, минус вправо.
- "turn": повернуться на месте. value — градусы: плюс налево, минус направо.
- "height": высота корпуса. value — метры от 0.3 (глубокий присед) до 0.8 (стоя). "присядь" = 0.5, "встань" = 0.8.
- "wait": пауза. value — секунды.
- "stop": остановиться. value = 0.
- "find": найти объект камерой (робот поворачивается, пока не увидит). value = 0.
- "approach": подойти к объекту по камере. value — на каком расстоянии остановиться, м (0 — по умолчанию, "вплотную" = 0.3).
- "push": подойти к мячу и толкнуть его вперёд. value — на сколько толкнуть, м (обычно 0.5, "сильно" = 1.0). Толкать можно только мяч.
- "look": посмотреть камерой. value = 0.
Для find, approach, push, look укажи поле target — что искать:
  "ball" — красный мяч; иначе английское название класса COCO: "person" (человек), "chair" (стул), "couch" (диван),
  "potted plant" (растение, цветок), "bottle" (бутылка), "cup" (кружка, чашка, стакан), "bowl" (миска), "banana" (банан),
  "apple" (яблоко), "suitcase" (чемодан), "backpack" (рюкзак), "laptop" (ноутбук), "vase" (ваза), "book" (книга) и другие из COCO.
  Для look: target "" — перечислить всё, что видно; конкретный target — сказать, где этот объект.
Если такого предмета нет среди классов COCO — честно скажи, что не умею его узнавать, steps пустой.
Необязательное поле speed: для walk и strafe в м/с (обычно 0.5, быстро 0.9, медленно 0.2), для turn в градусах в секунду (обычно 45, быстро 80, медленно 20). Пиши speed, только если пользователь просит быстрее или медленнее.
Если расстояние не указано — 1 метр. "шаг" — 0.3 метра. Если угол не указан — 90 градусов. "развернись" — 180 градусов.
reply — одна короткая фраза по-русски о том, что сделаешь.
Если команда не про движение — steps пустой, а в reply скажи, что умеешь ходить, поворачиваться и приседать.

Примеры:
"пройди два метра вперёд" -> {"reply": "Иду вперёд 2 м", "steps": [{"action": "walk", "value": 2}]}
"повернись направо" -> {"reply": "Поворачиваю направо на 90°", "steps": [{"action": "turn", "value": -90}]}
"отойди назад на полметра и присядь" -> {"reply": "Отхожу на 0.5 м и приседаю", "steps": [{"action": "walk", "value": -0.5}, {"action": "height", "value": 0.5}]}
"шагни влево" -> {"reply": "Шаг влево", "steps": [{"action": "strafe", "value": 0.3}]}
"быстро беги вперёд три метра" -> {"reply": "Быстро иду вперёд 3 м", "steps": [{"action": "walk", "value": 3, "speed": 0.9}]}
"пройди квадрат со стороной метр" -> {"reply": "Прохожу квадрат 1×1 м", "steps": [{"action": "walk", "value": 1}, {"action": "turn", "value": 90}, {"action": "walk", "value": 1}, {"action": "turn", "value": 90}, {"action": "walk", "value": 1}, {"action": "turn", "value": 90}, {"action": "walk", "value": 1}, {"action": "turn", "value": 90}]}
"найди мяч и подойди к нему" -> {"reply": "Ищу мяч и подхожу", "steps": [{"action": "find", "value": 0, "target": "ball"}, {"action": "approach", "value": 0, "target": "ball"}]}
"подойди к мячу вплотную" -> {"reply": "Подхожу к мячу вплотную", "steps": [{"action": "approach", "value": 0.3, "target": "ball"}]}
"пни мяч" -> {"reply": "Толкаю мяч", "steps": [{"action": "push", "value": 0.5, "target": "ball"}]}
"толкни мяч посильнее, потом отойди назад" -> {"reply": "Толкаю мяч на метр и отхожу", "steps": [{"action": "push", "value": 1.0, "target": "ball"}, {"action": "walk", "value": -1}]}
"подойди к человеку" -> {"reply": "Подхожу к человеку", "steps": [{"action": "approach", "value": 0, "target": "person"}]}
"найди стул" -> {"reply": "Ищу стул", "steps": [{"action": "find", "value": 0, "target": "chair"}]}
"найди бутылку и встань в полуметре от неё" -> {"reply": "Ищу бутылку и подхожу на 0.5 м", "steps": [{"action": "find", "value": 0, "target": "bottle"}, {"action": "approach", "value": 0.5, "target": "bottle"}]}
"где кружка?" -> {"reply": "Ищу кружку взглядом", "steps": [{"action": "look", "value": 0, "target": "cup"}]}
"что ты видишь?" -> {"reply": "Смотрю камерой", "steps": [{"action": "look", "value": 0, "target": ""}]}
"подойди к цилиндру" -> {"reply": "Цилиндр я пока не умею узнавать — только предметы из COCO и красный мяч", "steps": []}
"как дела?" -> {"reply": "Я умею ходить, поворачиваться, приседать и находить предметы камерой. Скажи, что делать.", "steps": []}"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ACTIONS},
                    "value": {"type": "number"},
                    "speed": {"type": "number"},
                    "target": {"type": "string", "enum": [""] + TARGETS},
                },
                "required": ["action", "value"],
            },
        },
    },
    "required": ["reply", "steps"],
}


# ----------------------------------------------------------------------------
# Qwen: локально через Ollama или по OpenAI-совместимому API
# ----------------------------------------------------------------------------
class LLMError(RuntimeError):
    pass


def _post_json(url: str, body: dict, headers: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"не могу подключиться к {url}: {e.reason}") from None
    except TimeoutError:
        raise LLMError(f"нет ответа за {timeout:.0f} с") from None


def extract_json(text: str) -> dict:
    """Достаёт JSON из ответа модели: убирает <think>...</think>, ```-обёртки и мусор вокруг."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"модель ответила не JSON: {text[:200]!r}")


class QwenPlanner:
    def __init__(self, backend: str, url: str, model: str, api_key: str | None,
                 timeout: float, use_gpu: bool, extra_body: dict | None):
        self.backend, self.url, self.model = backend, url.rstrip("/"), model
        self.api_key, self.timeout, self.use_gpu = api_key, timeout, use_gpu
        self.extra_body = extra_body or {}
        self._think_supported = True        # отключаем «размышления» Qwen3, если сервер это понимает
        self._json_mode_supported = True

    @classmethod
    def from_config(cls, cfg) -> QwenPlanner:
        """Из g1_studio.config.Config."""
        q = cfg.qwen
        extra = None
        if q.extra_body.strip():
            try:
                extra = json.loads(q.extra_body)
            except json.JSONDecodeError as e:
                raise LLMError(f"«Доп. поля запроса» — не JSON: {e}") from None
            if not isinstance(extra, dict):
                raise LLMError("«Доп. поля запроса» должны быть JSON-объектом {...}")
        return cls(q.backend, q.url, q.model, cfg.api_key(), q.timeout, q.gpu, extra)

    @property
    def where(self) -> str:
        if self.backend == "ollama":
            return "локально, " + ("GPU" if self.use_gpu else "CPU")
        return "API"

    # --- проверка при старте ---
    def list_models(self) -> list[str]:
        """Модели, скачанные в Ollama."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=5) as r:
                return [m.get("name", "") for m in json.loads(r.read().decode()).get("models", [])]
        except Exception:
            raise LLMError(f"Ollama не отвечает на {self.url}. Запусти: ollama serve  "
                           "(или sudo systemctl start ollama)") from None

    def check(self) -> None:
        if self.backend != "ollama":
            if not self.api_key:
                raise LLMError("нет ключа API: задай QWEN_API_KEY или впиши ключ в настройках")
            return
        names = self.list_models()
        want = self.model if ":" in self.model else self.model + ":latest"
        if want not in names:
            raise LLMError(f"модели {self.model} нет в Ollama. Скачай: ollama pull {self.model}")

    # --- вызовы ---
    def _ollama(self, messages: list) -> str:
        body = {
            "model": self.model, "messages": messages, "stream": False,
            "format": PLAN_SCHEMA,                      # ответ строго по схеме
            "keep_alive": "30m",                        # не выгружать модель между командами
            "options": {"temperature": 0, **({} if self.use_gpu else {"num_gpu": 0})},  # CPU: VRAM остаётся Isaac Sim
        }
        if self._think_supported:
            body["think"] = False
        try:
            r = _post_json(f"{self.url}/api/chat", body, None, self.timeout)
        except LLMError as e:
            if self._think_supported and "think" in str(e).lower():
                self._think_supported = False           # старая Ollama или модель без режима размышлений
                return self._ollama(messages)
            raise
        return r.get("message", {}).get("content", "")

    def _openai(self, messages: list) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0, **self.extra_body}
        if self._json_mode_supported:
            body["response_format"] = {"type": "json_object"}
        try:
            r = _post_json(f"{self.url}/chat/completions", body,
                           {"Authorization": f"Bearer {self.api_key}"}, self.timeout)
        except LLMError as e:
            if self._json_mode_supported and "response_format" in str(e):
                self._json_mode_supported = False
                return self._openai(messages)
            raise
        try:
            return r["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"неожиданный ответ API: {str(r)[:200]}") from None

    def plan(self, text: str) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
        call = self._ollama if self.backend == "ollama" else self._openai
        raw = call(messages)
        try:
            return extract_json(raw)
        except LLMError:
            # одна повторная попытка с напоминанием
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "Ответь только JSON вида {\"reply\": ..., \"steps\": [...]}."}]
            return extract_json(call(messages))


# ----------------------------------------------------------------------------
# Проверка плана: всё, что пришло от модели, обрезаем по безопасным пределам
# ----------------------------------------------------------------------------
@dataclass
class Step:
    action: str
    value: float
    speed: float = 0.0
    target: str = ""        # для камеры: "ball", класс COCO или "" (look: всё подряд)

    def describe(self) -> str:
        if self.action == "walk":
            return f"{'вперёд' if self.value >= 0 else 'назад'} {abs(self.value):.2f} м ({self.speed:.2f} м/с)"
        if self.action == "strafe":
            return f"{'влево' if self.value >= 0 else 'вправо'} {abs(self.value):.2f} м ({self.speed:.2f} м/с)"
        if self.action == "turn":
            return f"поворот {'налево' if self.value >= 0 else 'направо'} {abs(self.value):.0f}° ({self.speed:.0f}°/с)"
        if self.action == "height":
            return f"высота {self.value:.2f} м"
        if self.action == "wait":
            return f"пауза {self.value:.1f} с"
        if self.action == "find":
            return f"ищу камерой: {ru(self.target)}"
        if self.action == "approach":
            return f"подхожу: {ru(self.target)} (встану в {self.value:.2f} м)"
        if self.action == "push":
            return f"толкаю мяч на {self.value:.2f} м"
        if self.action == "look":
            return f"смотрю камерой{': ' + ru(self.target) if self.target else ''}"
        return "стоп"


def _num(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def validate_plan(plan: dict) -> tuple[str, list[Step], list[str]]:
    warnings: list[str] = []
    reply = str(plan.get("reply", "")).strip() if isinstance(plan, dict) else ""
    raw_steps = plan.get("steps", []) if isinstance(plan, dict) else []
    if not isinstance(raw_steps, list):
        raw_steps = []
    if len(raw_steps) > MAX_STEPS:
        warnings.append(f"в плане {len(raw_steps)} шагов — выполню первые {MAX_STEPS}")
        raw_steps = raw_steps[:MAX_STEPS]

    steps: list[Step] = []
    for i, s in enumerate(raw_steps, 1):
        if not isinstance(s, dict) or s.get("action") not in ACTIONS:
            warnings.append(f"шаг {i}: непонятное действие {s!r} — пропускаю")
            continue
        a = s["action"]
        v = _num(s.get("value"), 0.0)
        sp = _num(s.get("speed"), 0.0) or 0.0

        def clamp(x, lo, hi, what, n=i):
            if x < lo or x > hi:
                warnings.append(f"шаг {n}: {what} {x:g} вне предела, беру {min(max(x, lo), hi):g}")
            return min(max(x, lo), hi)

        if a in ("walk", "strafe"):
            v = clamp(v, -MAX_DISTANCE_M, MAX_DISTANCE_M, "расстояние")
            if abs(v) < 0.01:
                continue
            vmax = (VX_FWD_MAX if v > 0 else VX_BACK_MAX) if a == "walk" else VY_MAX
            sp = clamp(sp, MIN_SPEED[a], vmax, "скорость") if sp > 0 else min(DEFAULTS[a], vmax)
        elif a == "turn":
            v = clamp(v, -MAX_ANGLE_DEG, MAX_ANGLE_DEG, "угол")
            if abs(v) < 1:
                continue
            sp = clamp(sp, MIN_SPEED[a], WZ_MAX_DEG, "скорость поворота") if sp > 0 else DEFAULTS[a]
        elif a == "height":
            v = clamp(v, H_MIN, H_MAX, "высота")
        elif a == "wait":
            v = clamp(v, 0.0, MAX_WAIT_S, "пауза")
        target = ""
        if a in VISION_ACTIONS:
            raw = str(s.get("target") or "").strip().lower()
            target = raw if raw in TARGETS else TARGET_RU.get(raw, raw)
            if target == "sports ball":
                target = "ball"                       # мяч надёжнее ищется по цвету
            if not target and a != "look":
                target = "ball"                       # как раньше: без цели — мяч
            if target and target not in TARGETS:
                warnings.append(f"шаг {i}: не умею узнавать «{raw}» — пропускаю")
                continue
            if a == "push" and target != "ball":
                warnings.append(f"шаг {i}: толкать умею только мяч, а не «{ru(target)}» — пропускаю")
                continue
        if a == "approach":
            default = APPROACH_DEFAULT if target == "ball" else APPROACH_OBJECT_DEFAULT
            v = clamp(v, APPROACH_MIN, APPROACH_MAX, "дистанция до цели") if v > 0 else default
        elif a == "push":
            v = clamp(v, PUSH_MIN, PUSH_MAX, "толчок") if v > 0 else PUSH_DEFAULT
        steps.append(Step(a, v, sp, target))
    return reply, steps, warnings


