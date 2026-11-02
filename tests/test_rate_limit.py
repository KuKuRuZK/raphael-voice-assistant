"""Ліміт Groq за хвилину: коротка пауза і повтор, запасна модель зі своїм лімітом,
репліки на окремій моделі й чесна фраза, коли не відповів ніхто."""
import types

import pytest

from conftest import fake_reply


class RateLimit(Exception):
    """Як openai.RateLimitError: текст з 429 і заголовок retry-after."""
    def __init__(self, wait=None, text="Error code: 429 - rate_limit_exceeded"):
        super().__init__(text)
        self.response = types.SimpleNamespace(headers={"retry-after": str(wait)} if wait is not None else {})


@pytest.fixture
def llm(env, monkeypatch):
    from raphael import llm
    env.patch("BRAIN_LOCAL_MODEL", "")
    env.patch("OFFLINE_MODEL", "")
    env.patch("GAME", "ARK")
    env.patch("GROQ_SPARE_MODEL", "spare")
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    llm.slept = slept
    return llm


@pytest.mark.parametrize("e,wait", [
    (RateLimit(2), 2.0),
    (RateLimit(text="429 Rate limit reached ... Please try again in 7.48s."), 7.48),
    (RateLimit(text="429 Rate limit reached ... Please try again in 1m30.5s."), 90.5),
    (RateLimit(), None),                                  # ліміт, але скільки чекати, не сказали
    (TimeoutError("Request timed out."), None),           # не ліміт
])
def test_retry_after(llm, e, wait):
    assert llm._retry_after(e) == wait


def test_spare_goes_right_after_primary(llm, env):
    primary, fallback = env.get("GROQ_PRIMARY_MODEL"), env.get("GROQ_FALLBACK_MODEL")
    assert llm._candidates(primary, None) == [primary, "spare", fallback]
    assert llm._candidates(primary, False) == [primary]                    # зір: без резерву


def _script(env, answers):
    """_llm_call віддає/кидає по черзі з answers[модель]."""
    calls = []

    def call(model, messages, **kw):
        calls.append(model)
        out = answers[model].pop(0)
        if isinstance(out, Exception):
            raise out
        return fake_reply(out)
    env.patch("_llm_call", call)
    return calls


def test_short_wait_retries_same_model(llm, env):
    primary = env.get("GROQ_PRIMARY_MODEL")
    calls = _script(env, {primary: [RateLimit(1.5), "Ага."]})
    assert llm.llm_chat(primary, [{"role": "user", "content": "x"}]).choices[0].message.content == "Ага."
    assert calls == [primary, primary] and llm.slept == [pytest.approx(1.7)]


def test_long_wait_goes_to_spare_at_once(llm, env):
    primary = env.get("GROQ_PRIMARY_MODEL")
    calls = _script(env, {primary: [RateLimit(40)], "spare": ["Ось."]})
    assert llm.llm_chat(primary, [{"role": "user", "content": "x"}]).choices[0].message.content == "Ось."
    assert calls == [primary, "spare"] and llm.slept == []


def test_minute_limit_is_said_honestly(llm, env):
    """2026-11-02 23:31: 429 на хвилину + резерв завис, а вона казала «денний ліміт вичерпано»."""
    from raphael import assistant
    primary, fallback = env.get("GROQ_PRIMARY_MODEL"), env.get("GROQ_FALLBACK_MODEL")
    _script(env, {primary: [RateLimit(40)], "spare": [RateLimit(35)], fallback: [TimeoutError("timed out")]})
    assert assistant.ask_lin("скільки тобі років?") == ""
    assert env.spoken and "хвилин" in env.spoken[-1] and "Денний" not in env.spoken[-1]


def test_daily_limit_still_said_as_daily(llm, env):
    e = llm.LLMUnavailable([("openai/gpt-oss-120b", RateLimit(3600 * 5))])
    assert e.rate_limited and not e.minute_limited


def test_vibe_line_uses_own_model_without_fallback_in_game(llm, env):
    from raphael import vibes
    env.patch("VIBE_MODEL", "qwen/x")
    calls = _script(env, {"qwen/x": ["Ну ти й упертий."]})
    assert vibes._line("Влад утретє програв боса.") == "Ну ти й упертий."
    assert calls == ["qwen/x"]


def test_vibe_line_stays_silent_when_its_model_is_limited(llm, env):
    from raphael import vibes
    env.patch("VIBE_MODEL", "qwen/x")
    calls = _script(env, {"qwen/x": [RateLimit(30)]})
    with pytest.raises(llm.LLMUnavailable):
        vibes._line("Влад утретє програв боса.")
    assert calls == ["qwen/x"]                  # ліміт відповідей Владу не чіпаємо
