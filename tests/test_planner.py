import pytest

from g1_studio.config import Config
from g1_studio.core.planner import (
    MAX_STEPS, LLMError, QwenPlanner, extract_json, validate_plan,
)


def test_validate_clamps_and_defaults():
    reply, steps, warnings = validate_plan({"reply": "ок", "steps": [
        {"action": "walk", "value": 9},                       # обрезать до 5 м
        {"action": "walk", "value": -1, "speed": 3},          # назад не быстрее 0.6
        {"action": "turn", "value": 90},
        {"action": "height", "value": 0.1},
        {"action": "fly", "value": 1},                        # такого нет
        {"action": "walk", "value": 0.001},                   # ничтожно — пропустить
    ]})
    assert reply == "ок"
    assert [s.action for s in steps] == ["walk", "walk", "turn", "height"]
    assert steps[0].value == 5.0 and steps[0].speed == 0.5
    assert steps[1].speed == 0.6
    assert steps[2].speed == 45.0
    assert steps[3].value == 0.3
    assert len(warnings) == 4


def test_validate_targets():
    _, steps, warnings = validate_plan({"steps": [
        {"action": "approach", "value": 0, "target": "человек"},   # по-русски -> person
        {"action": "find", "value": 0},                             # без цели -> мяч
        {"action": "approach", "value": 0, "target": "sports ball"},
        {"action": "push", "value": 0, "target": "chair"},          # толкать только мяч
        {"action": "look", "value": 0, "target": "цилиндр"},        # не умеем
        {"action": "look", "value": 0, "target": ""},
    ]})
    assert [(s.action, s.target) for s in steps] == [
        ("approach", "person"), ("find", "ball"), ("approach", "ball"), ("look", "")]
    assert steps[0].value == 0.7 and steps[2].value == 0.5      # от объектов дальше, чем от мяча
    assert len(warnings) == 2


def test_validate_too_many_steps_and_garbage():
    _, steps, warnings = validate_plan({"steps": [{"action": "wait", "value": 1}] * 20})
    assert len(steps) == MAX_STEPS and warnings
    assert validate_plan("мусор") == ("", [], [])
    assert validate_plan({"steps": "не список"})[1] == []


@pytest.mark.parametrize("raw", [
    '{"reply": "x", "steps": []}',
    '<think>хм</think>{"reply": "x", "steps": []}',
    '```json\n{"reply": "x", "steps": []}\n```',
    'Вот план: {"reply": "x", "steps": []} — готово',
])
def test_extract_json(raw):
    assert extract_json(raw)["reply"] == "x"


def test_extract_json_fails():
    with pytest.raises(LLMError):
        extract_json("никакого json")


def test_ollama_backend(fake_llm):
    fake_llm.plans["иди"] = {"reply": "иду", "steps": [{"action": "walk", "value": 1}]}
    p = QwenPlanner("ollama", fake_llm.url, "qwen3:1.7b", None, 5, False, None)
    p.check()
    assert p.plan("иди")["reply"] == "иду"
    body = fake_llm.requests[-1]["body"]
    assert body["options"]["num_gpu"] == 0           # по умолчанию CPU: видеопамять — Isaac Sim
    assert body["think"] is False and "format" in body


def test_ollama_missing_model(fake_llm):
    p = QwenPlanner("ollama", fake_llm.url, "qwen3:4b", None, 5, False, None)
    with pytest.raises(LLMError, match="ollama pull qwen3:4b"):
        p.check()


def test_ollama_down():
    p = QwenPlanner("ollama", "http://127.0.0.1:9", "qwen3:1.7b", None, 2, False, None)
    with pytest.raises(LLMError, match="Ollama не отвечает"):
        p.check()


def test_api_backend(fake_llm):
    fake_llm.plans["стой"] = {"reply": "стою", "steps": [{"action": "stop", "value": 0}]}
    p = QwenPlanner("api", fake_llm.url, "qwen-plus", "KEY", 5, False, {"enable_thinking": False})
    assert p.plan("стой")["reply"] == "стою"
    req = fake_llm.requests[-1]
    assert req["path"].endswith("/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer KEY"
    assert req["body"]["enable_thinking"] is False


def test_api_needs_key():
    with pytest.raises(LLMError, match="ключа"):
        QwenPlanner("api", "http://x", "qwen-plus", "", 5, False, None).check()


def test_retry_on_non_json(fake_llm):
    fake_llm.raw = "не json"
    p = QwenPlanner("ollama", fake_llm.url, "qwen3:1.7b", None, 5, False, None)
    with pytest.raises(LLMError, match="не JSON"):
        p.plan("иди")
    assert len(fake_llm.requests) == 2               # была повторная попытка с напоминанием


def test_from_config(monkeypatch):
    cfg = Config()
    cfg.qwen.backend, cfg.qwen.extra_body = "api", '{"enable_thinking": false}'
    monkeypatch.setenv("QWEN_API_KEY", "env-key")
    p = QwenPlanner.from_config(cfg)
    assert p.api_key == "env-key" and p.extra_body == {"enable_thinking": False}
    cfg.qwen.extra_body = "{кривой"
    with pytest.raises(LLMError):
        QwenPlanner.from_config(cfg)
