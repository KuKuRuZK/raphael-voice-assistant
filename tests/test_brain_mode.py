"""Мозок за режимом: локальна модель поза грою, хмара в грі, памʼять звільняється під гру."""
import time

import pytest

LOCAL = "lmstudio:google/gemma-4-26b-a4b-qat"


@pytest.fixture
def brain(env):
    from raphael import llm
    env.patch("BRAIN_LOCAL_MODEL", LOCAL)
    env.patch("GAME", None)
    env.patch("FAST_MODEL", "")
    return llm


def test_local_model_outside_game(brain, env):
    assert brain.main_model() == LOCAL
    assert brain._choose_model("розкажи щось цікаве") == LOCAL


def test_cloud_model_in_game(brain, env):
    env.patch("GAME", "ARK")
    assert brain.main_model() == env.get("GROQ_PRIMARY_MODEL")


def test_no_local_model_is_cloud_as_before(brain, env):
    env.patch("BRAIN_LOCAL_MODEL", "")
    assert brain.main_model() == env.get("GROQ_PRIMARY_MODEL")


def test_local_falls_back_to_both_clouds(brain, env):
    cloud = [env.get("GROQ_PRIMARY_MODEL"), env.get("GROQ_FALLBACK_MODEL")]
    assert brain._candidates(LOCAL, None) == [LOCAL, *cloud]
    assert brain._candidates(LOCAL, cloud[0]) == [LOCAL, *cloud]       # так кличе assistant


@pytest.mark.parametrize("text", ["нагадай через 10 хвилин подзвонити мамі", "відкрий хром", "потанцюй",
                                  "запиши в мозок купити молоко", "наступний трек", "сядь на вікно"])
def test_commands_go_to_cloud_even_outside_game(brain, env, text):
    assert brain._choose_model(text) == env.get("GROQ_PRIMARY_MODEL")


@pytest.mark.parametrize("text", ["як справи", "скільки буде 17 на 23", "що порадиш подивитись увечері"])
def test_talk_stays_local(brain, text):
    assert brain._choose_model(text) == LOCAL


def test_gemma4_does_not_think_in_lm_studio(brain):
    assert brain._model_kwargs("google/gemma-4-26b-a4b-qat") == {"reasoning_effort": "none"}
    assert brain._model_kwargs("gemma-4-26b-a4b-it") == {}          # той самий Gemma через Gemini API не чіпаємо


def test_unload_lm_studio(brain, monkeypatch):
    calls = []
    monkeypatch.setattr(brain, "_lms", lambda *a, **k: calls.append(a) or True)
    assert brain.local_unload() is True
    assert calls == [("unload", "--all")]


def test_preload_skips_loaded_model(brain, monkeypatch):
    import subprocess

    class R:
        stdout = "google/gemma-4-26b-a4b-qat    IDLE    15.63 GB"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(brain, "_lms", lambda *a, **k: pytest.fail("друга копія моделі не потрібна"))
    assert brain.local_preload() is True


def test_preload_loads_with_context(brain, monkeypatch, env):
    import subprocess

    class R:
        stdout = "No models are currently loaded."
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(brain, "_lms", lambda *a, **k: calls.append(a) or True)
    assert brain.local_preload() is True
    assert calls == [("server", "start"),
                     ("load", "google/gemma-4-26b-a4b-qat", "-c", str(env.get("BRAIN_LOCAL_CONTEXT")), "--gpu", "max",
                      "--parallel", "1", "-y")]


def test_game_start_unloads_and_end_preloads(brain, env, monkeypatch):
    from raphael import avatar, runtime
    monkeypatch.setattr(avatar, "post_json", lambda *a, **k: {})
    monkeypatch.setattr(avatar, "window_rect", lambda pids: [0, 0, 2560, 1440])
    monkeypatch.setitem(avatar._last, "mode", "normal")
    done = []
    monkeypatch.setattr(brain, "local_unload", lambda: done.append("unload"))
    monkeypatch.setattr(brain, "local_preload", lambda: done.append("preload"))

    avatar.tick("ARK", [1])
    assert runtime.GAME == "ARK" and brain.main_model() == env.get("GROQ_PRIMARY_MODEL")
    avatar.tick(None, [])
    assert runtime.GAME is None and brain.main_model() == LOCAL
    for _ in range(50):
        if len(done) == 2:
            break
        time.sleep(0.02)
    assert done == ["unload", "preload"]


def test_same_mode_does_not_reload(brain, env, monkeypatch):
    from raphael import avatar
    monkeypatch.setattr(avatar, "post_json", lambda *a, **k: {})
    monkeypatch.setitem(avatar._last, "mode", "normal")
    monkeypatch.setattr(avatar, "brain_switch", lambda: pytest.fail("режим той самий, перемикати нічого"))
    avatar.tick(None, [])


# ── Запобіжники для слабкої локальної моделі ──────────────────────────────────
def _reply(text):
    import types
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text, tool_calls=None))])


@pytest.mark.parametrize("bad", ["Вільнюс — столица Литвы.", "", "   ",
                                 "note_remind:подзвонити мамі|10; [ACTION:brain_add:подзвонити мамі]",
                                 "open_app:Chrome [ACTION:app:open]",
                                 "[dance] [ACTION:avatar:dance]",
                                 "Окей, ставлю note_remind:мама|10"])
def test_bad_local_reply_goes_to_cloud(brain, env, monkeypatch, bad):
    asked = []

    def call(m, messages, **kw):
        asked.append(m)
        return _reply(bad if m == LOCAL else "Вільнюс, столиця Литви.")
    monkeypatch.setattr(brain, "_llm_call", call)
    r = brain.llm_chat(LOCAL, [{"role": "user", "content": "столиця Литви"}])
    assert r.choices[0].message.content == "Вільнюс, столиця Литви."
    assert asked == [LOCAL, env.get("GROQ_PRIMARY_MODEL")]


@pytest.mark.parametrize("good", ["391, ну.", "Відкриваю Chrome [ACTION:open_app:chrome]",
                                  "note_remind:подзвонити мамі|10", "Танцюю! [ACTION:avatar:dance]",
                                  "О 9:30 у тебе дзвінок, не забудь."])
def test_good_local_reply_stays_local(brain, monkeypatch, good):
    monkeypatch.setattr(brain, "_llm_call", lambda m, messages, **kw: _reply(good))
    assert brain.llm_chat(LOCAL, [{"role": "user", "content": "щось"}]).choices[0].message.content == good


def test_bare_avatar_tag_is_an_action(brain):
    assert brain._find_action("avatar:dance")[:2] == ("avatar", "dance")
    assert brain._find_action("[Action:avatar:sitA]")[:2] == ("avatar", "sitA")


def test_cloud_silent_local_is_last_hope(brain, env):
    cloud = [env.get("GROQ_PRIMARY_MODEL"), env.get("GROQ_FALLBACK_MODEL")]
    assert brain._candidates(cloud[0], cloud[1]) == [*cloud, LOCAL]
    env.patch("GAME", "ARK")                                    # у грі локальна вивантажена: її не питати
    assert LOCAL not in brain._candidates(cloud[0], cloud[1])
