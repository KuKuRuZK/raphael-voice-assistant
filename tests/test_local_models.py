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



def _flaky_client(failures, calls):
    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, messages, **kw):
                    calls.append(model)
                    if len(calls) <= failures:
                        raise Exception("Error code: 400 - The model produced output that does not "
                                        "match the expected peg-native format")
                    return fake_reply("Ага.")
    return Client


@pytest.mark.parametrize("failures,ok", [(1, True), (2, True), (3, False)])
def test_local_broken_format_is_asked_again(lin, env, failures, ok):
    """Модель у LM Studio ламає службовий формат (400): двічі питаємо ще раз (gpt-oss йде своїм шаблоном)."""
    from raphael import llm
    calls = []
    env.patch("_llm", lambda model: (_flaky_client(failures, calls), model.partition(":")[2]))
    if ok:
        assert llm._llm_call("lmstudio:google/gemma-4", []).choices[0].message.content == "Ага."
    else:
        with pytest.raises(Exception, match="peg-native"):
            llm._llm_call("lmstudio:google/gemma-4", [])
    assert len(calls) == min(failures + 1, 3)


def test_cloud_broken_format_is_not_repeated(lin, env):
    from raphael import llm
    calls = []
    env.patch("_llm", lambda model: (_flaky_client(1, calls), model))
    with pytest.raises(Exception):
        llm._llm_call("openai/gpt-oss-120b", [])
    assert len(calls) == 1                     # хмару не смикаємо: там резервна модель


@pytest.mark.parametrize("text", ["знайди скільки коштує rtx 5090", "пошукай рецепт борщу", "загугли курс злотого"])
def test_search_goes_to_cloud(lin, env, text):
    from raphael import llm
    env.patch("BRAIN_LOCAL_MODEL", "lmstudio:openai/gpt-oss-20b")
    env.patch("GAME", None)
    env.patch("FAST_MODEL", "")
    assert llm._choose_model(text) == env.get("GROQ_PRIMARY_MODEL")


@pytest.mark.parametrize("raw,expected", [
    ("<|channel|>analysis<|message|>Need a short answer.<|end|><|start|>assistant<|channel|>final<|message|>[EMO:sly] Привіт.",
     "[EMO:sly] Привіт."),
    # те, на чому падав розбір LM Studio (лог сервера 2026-11-03)
    ("<|channel|>final <|constrain|>[EMO:sly] [ACTION:window_snap:left]", "[EMO:sly] [ACTION:window_snap:left]"),
    ("<|channel|>final <|constrain|>EMO:sly<|message|>Залишайся, я вже лайкаю.", "Залишайся, я вже лайкаю."),
    ("<|channel|>final <|constrain|>json<|message|>[EMO:happy] Ок.", "[EMO:happy] Ок."),
    ("<|channel|>analysis<|message|>Thinking and thinking and", ""),          # роздуми зʼїли ліміт
    # «виклик функції» каналом commentary замість відповіді
    ('<|channel|>analysis<|message|>Use get_time.<|end|><|start|>assistant<|channel|>commentary to=assistant '
     '<|constrain|>response_type=action code<|message|>{"name":"get_time"}', "[ACTION:get_time:]"),
    ('<|channel|>commentary to=functions.weather <|constrain|>json<|message|>{"name":"weather","arguments":{"city":"Berlin"}}',
     "[ACTION:weather:Berlin]"),
    ("<|channel|>commentary to=assistant<|message|>[EMO:sly] Щось гучно.", "[EMO:sly] Щось гучно."),
])
def test_harmony_final(lin, raw, expected):
    from raphael import llm
    assert llm._harmony_final(raw) == expected


def test_harmony_prompt_roles(lin):
    from raphael import llm
    p = llm._harmony_prompt([{"role": "system", "content": "Ти Лін."}, {"role": "user", "content": "привіт"},
                             {"role": "assistant", "content": "Ага."}, {"role": "user", "content": "котра година"}])
    assert "<|start|>developer<|message|># Instructions\n\nТи Лін.<|end|>" in p
    assert "<|start|>assistant<|channel|>final<|message|>Ага.<|end|>" in p
    assert p.endswith("<|start|>user<|message|>котра година<|end|><|start|>assistant")


def test_local_gpt_oss_goes_through_completions(lin, env):
    import types
    from raphael import llm
    sent = []

    class Client:
        class completions:
            @staticmethod
            def create(model, prompt, **kw):
                sent.append((model, kw))
                return types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
                    finish_reason="stop", text="<|channel|>final<|message|>[EMO:happy] Привіт, Владе!")])
    env.patch("_llm", lambda model: (Client, model.partition(":")[2]))
    resp = llm.llm_chat("lmstudio:openai/gpt-oss-20b", [{"role": "user", "content": "дарова"}], fallback=False)
    assert resp.choices[0].message.content == "[EMO:happy] Привіт, Владе!"
    assert sent[0][0] == "openai/gpt-oss-20b" and "<|return|>" in sent[0][1]["stop"]
    assert "".join(llm.llm_stream("lmstudio:openai/gpt-oss-20b", [{"role": "user", "content": "x"}],
                                  fallback=False)) == "[EMO:happy] Привіт, Владе!"


def test_action_tag_without_param_is_a_good_local_reply(lin):
    from raphael import llm
    llm._check_local_reply(fake_reply("[EMO:happy] [ACTION:get_time]"))           # не BadLocalReply


def _real_local_ready(env):
    return next(v for m, n, v in env._saved if n == "_local_ready")


@pytest.mark.parametrize("state,ready", [("loaded", True), ("not-loaded", False)])
def test_unloaded_model_is_not_woken_by_a_request(lin, env, monkeypatch, state, ready):
    """Запит до вивантаженої моделі LM Studio вантажить її сам, з копією ваг у RAM (14,7 ГБ
    2026-11-07). Тепер такий запит не йде, а модель вантажиться економно у фоні."""
    import io
    import json
    import threading
    from raphael import llm
    env.patch("BRAIN_LOCAL_MODEL", "lmstudio:google/gemma-4-26b-a4b-qat")
    env.patch("GAME", None)
    body = json.dumps({"data": [{"id": "google/gemma-4-26b-a4b-qat", "state": state}]}).encode()
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(body))
    reloaded = threading.Event()
    env.patch("local_preload", lambda: reloaded.set())
    assert _real_local_ready(env)("lmstudio:google/gemma-4-26b-a4b-qat") is ready
    assert reloaded.wait(2) is (not ready)


def test_lm_studio_silent_means_try_anyway(lin, env, monkeypatch):
    from raphael import llm

    def down(url, timeout=0):
        raise OSError("refused")
    monkeypatch.setattr(llm.urllib.request, "urlopen", down)
    assert _real_local_ready(env)("lmstudio:google/gemma-4-26b-a4b-qat") is True
    assert _real_local_ready(env)("groq:openai/gpt-oss-120b") is True
