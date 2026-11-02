"""«Глянь на екран»: запасна модель із зором і відповідь без розмітки."""
import pytest

from conftest import fake_reply


@pytest.fixture
def look(env):
    from raphael import pc
    env.patch("_capture_screen_b64", lambda *a, **k: "AAAA")
    env.patch("VISION_MODEL", "gemini:vision")
    env.patch("VISION_FALLBACK_MODEL", "qwen/vision")
    return pc


def _models(env, answers):
    asked = []

    def chat(model, messages, fallback=None, **kw):
        asked.append((model, fallback))
        out = answers[model]
        if isinstance(out, Exception):
            raise out
        return fake_reply(out)
    env.patch("llm_chat", chat)
    return asked


def test_answer_is_spoken_without_markdown(look, env):
    asked = _models(env, {"gemini:vision": "Це `ModuleNotFoundError`: нема **pytest**."})
    look._screen_look("що це за помилка")
    assert env.spoken == ["Це ModuleNotFoundError: нема pytest."]
    assert asked == [("gemini:vision", False)]          # звичайний резерв картинок не бачить


def test_falls_back_to_second_vision_model(look, env):
    asked = _models(env, {"gemini:vision": TimeoutError("timed out"), "qwen/vision": "Бачу термінал."})
    look._screen_look("")
    assert [m for m, _ in asked] == ["gemini:vision", "qwen/vision"] and env.spoken == ["Бачу термінал."]


def test_says_honestly_when_nobody_sees(look, env):
    _models(env, {"gemini:vision": TimeoutError("timed out"), "qwen/vision": RuntimeError("503")})
    look._screen_look("")
    assert "не відповіли" in env.spoken[-1]
