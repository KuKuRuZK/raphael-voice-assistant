"""Локальні моделі: LM Studio як провайдер і роздуми, яких не можна зачитувати."""
import pytest

from conftest import fake_reply


@pytest.mark.parametrize("text,expected", [
    ("Привіт", "Привіт"),
    ("<thought>The user asks about time.</thought>Зараз восьма.", "Зараз восьма."),
    ("<think>\nlong\nreasoning\n</think>\n\nОкей, вмикаю.", "Окей, вмикаю."),
    ("<THINK>x</THINK>Так", "Так"),
    # Обрізано лімітом токенів посеред роздумів: краще мовчати, ніж читати їх
    ("<think>I need to check the", ""),
    ("2 < 3, а <b>жирне</b> лишається", "2 < 3, а <b>жирне</b> лишається"),
    ("", ""),
    (None, None),
])
def test_strip_thinking(lin, text, expected):
    from raphael import llm
    assert llm.strip_thinking(text) == expected


@pytest.mark.parametrize("chunks,expected", [
    (["Зараз ", "восьма."], "Зараз восьма."),
    (["<tho", "ught>The user", " asks</tho", "ught>Зараз", " восьма."], "Зараз восьма."),
    (["Ну <", "think>hmm</think> добре"], "Ну добре"),
    (["<think>never closed"], ""),
    (["2 <", " 3"], "2 < 3"),
    (["кінець <"], "кінець <"),
])
def test_stream_without_thinking(lin, chunks, expected):
    from raphael import llm
    assert "".join(llm._without_thinking(iter(chunks))) == expected


def test_llm_chat_strips_thinking_and_lmstudio_is_local(lin, env):
    from raphael import llm
    calls = []

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, messages, **kw):
                    calls.append((model, kw))
                    return fake_reply("<thought>planning</thought>Окей.")

    env.patch("_llm", lambda model: (Client, model.partition(":")[2]))
    resp = llm.llm_chat("lmstudio:google/gemma-4", [{"role": "user", "content": "x"}],
                        fallback=False, tools=[{"type": "function"}])
    assert resp.choices[0].message.content == "Окей."
    assert calls[0][0] == "google/gemma-4"
    assert "tools" not in calls[0][1]          # локальним функції не передаємо
    assert llm._is_local("lmstudio:x") and llm._is_local("local:x")
    assert not llm._is_local("groq:x")
    assert "lmstudio" in llm.LLM_PROVIDERS


def test_lmstudio_errors_do_not_hide_cloud_rate_limit(lin):
    from raphael import llm
    e = llm.LLMUnavailable([("openai/gpt-oss-120b", Exception("429 rate_limit")),
                            ("lmstudio:gemma", ConnectionError("refused"))])
    assert e.rate_limited

