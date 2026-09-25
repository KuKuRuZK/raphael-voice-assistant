"""Лічильник денних лімітів: рахує, попереджає раз, переживає перезапуск."""
import json
import types

import pytest

from raphael import voice_rules as vr


@pytest.fixture
def said(env):
    out = []
    env.patch("_announce", out.append)
    env.patch("GROQ_PRIMARY_MODEL", "openai/gpt-oss-120b")
    env.patch("GROQ_FALLBACK_MODEL", "gemini:gemini-3.1-flash-lite")
    env.patch("VISION_MODEL", "gemini:gemini-3.1-flash-lite")
    return out


def test_report_counts_requests_tokens_and_audio(env, said):
    record = env.get("record")
    record("openai/gpt-oss-120b", tokens=1500)
    record("openai/gpt-oss-120b", tokens=1500)
    record("groq:whisper-large-v3-turbo", seconds=3.2)
    record("gemini:gemini-3.1-flash-lite")
    assert env.get("report_text")() == (
        "За моїм підрахунком сьогодні: основна модель: 2 запити з 1000, 3000 токенів з 200 тисяч; "
        "розпізнавання мови: 1 запит з 2000, 1 хвилина аудіо з 480; "
        "резервна модель і зір: 1 запит з 500.")
    assert said == []


def test_warns_once_at_80_and_once_at_100(env, said):
    env.patch("DAILY_LIMITS", {"groq:m": {"requests": 5}})
    for _ in range(6):
        env.get("record")("m")
    assert said == ["m: витрачено вже 80% денного ліміту запитів.",
                    "m: денний ліміт запитів вичерпано, за моїм підрахунком. Далі працюватиме резерв."]


def test_survives_restart_and_resets_next_day(env, said, tmp_path):
    env.get("record")("openai/gpt-oss-120b", tokens=100)
    env.patch("_state", None)                                   # перезапуск Рафаеля
    assert "основна модель: 1 запит" in env.get("report_text")()
    path = tmp_path / "usage.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["date"] = "2000-01-01"                                # учорашній файл
    path.write_text(json.dumps(state), encoding="utf-8")
    env.patch("_state", None)
    assert env.get("report_text")() == "Сьогодні я ще не зверталась до моделей."


def _reply(text, total_tokens):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        usage=types.SimpleNamespace(total_tokens=total_tokens))


def test_model_calls_are_counted(env, said):
    def create(model, messages, stream=False, **kw):
        if not stream:
            return _reply("Привіт.", 1234)
        delta = types.SimpleNamespace(content="Привіт.", reasoning=None)
        return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)]),
                     types.SimpleNamespace(choices=[], x_groq={"usage": {"total_tokens": 900}})])
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    env.patch("_llm", lambda m: (client, m.split(":", 1)[-1]))
    env.get("llm_chat")("openai/gpt-oss-120b", [])
    assert "".join(env.get("llm_stream")("openai/gpt-oss-120b", [])) == "Привіт."
    used = env.get("_state")["counts"]["groq:openai/gpt-oss-120b"]
    assert (used["requests"], used["tokens"]) == (2, 2134)


def test_failed_call_is_not_counted(env, said):
    def create(**kw):
        raise TimeoutError("read timeout")
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    env.patch("_llm", lambda m: (client, m))
    with pytest.raises(env.get("LLMUnavailable")):
        env.get("llm_chat")("openai/gpt-oss-120b", [])
    assert env.get("report_text")() == "Сьогодні я ще не зверталась до моделей."


@pytest.mark.parametrize("text", ["скільки лімітів лишилось", "Ліміти?", "які ліміти"])
def test_usage_phrases(text):
    assert vr.instant_command(text) == "usage_report"
