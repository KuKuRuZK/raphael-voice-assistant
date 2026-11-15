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
                     ("load", "google/gemma-4-26b-a4b-qat", "-c", str(env.get("BRAIN_LOCAL_CONTEXT")), "--gpu", "0.7",
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
                                 "Окей, ставлю note_remind:мама|10",
                                 # Локальна веде лише розмову (команди йдуть у хмару): справжня дія
                                 # від неї означає, що команду не впізнали, хай вирішує хмара
                                 "Відкриваю Chrome [ACTION:open_app:chrome]", "note_remind:подзвонити мамі|10",
                                 "Та це космічна діра, нахрін. [web_search:що таке чорна діра]"])
def test_bad_local_reply_goes_to_cloud(brain, env, monkeypatch, bad):
    asked = []

    def call(m, messages, **kw):
        asked.append(m)
        return _reply(bad if m == LOCAL else "Вільнюс, столиця Литви.")
    monkeypatch.setattr(brain, "_llm_call", call)
    r = brain.llm_chat(LOCAL, [{"role": "user", "content": "столиця Литви"}])
    assert r.choices[0].message.content == "Вільнюс, столиця Литви."
    assert asked == [LOCAL, env.get("GROQ_PRIMARY_MODEL")]


@pytest.mark.parametrize("good", ["391, ну.", "Танцюю! [ACTION:avatar:dance]",
                                  "О 9:30 у тебе дзвінок, не забудь.", "Зараз гляну. [ACTION:calendar_tomorrow:]",
                                  # теги тіла й вигадані Gemma дописує до розмови: їх прибере assistant
                                  "[EMO:smug] Та норм усе. [ACTION:avatar:stretch]",
                                  "[EMO:happy] Доброго, соня! [ACTION:stretch:потягнутись]",
                                  "[EMO:sly] Та що завгодно, аби не нудне. [ACTION:search_web:що подивит"])
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



@pytest.mark.parametrize("reply,expected", [
    ("[EMO:smug] Та норм усе. [ACTION:avatar:stretch]", "[EMO:smug] Та норм усе."),
    ("[EMO:happy] Доброго, соня! [ACTION:stretch:потягнутись]", "[EMO:happy] Доброго, соня!"),
    ("Не дуже. [ACTION:peek]", "Не дуже."),
    ("Ну шо [ACTION:search_web:що подивит", "Ну шо"),
    ("О 9:30 дзвінок. [ACTION:calendar_today:]", "О 9:30 дзвінок. [ACTION:calendar_today:]"),
])
def test_drop_unasked_tags(brain, reply, expected):
    assert brain.drop_unasked_tags(reply) == expected


def test_local_talk_reply_is_spoken_without_body_tag(brain, env, monkeypatch):
    from conftest import fake_reply
    from raphael import assistant
    monkeypatch.setattr(brain, "_llm_call", lambda m, messages, **kw: fake_reply("[EMO:smug] Та норм усе. [ACTION:avatar:stretch]"))
    env.patch("AVATAR_ENABLED", False)
    assistant.ask_lin("як справи?")
    assert assistant.history[-1]["content"] == "[EMO:smug] Та норм усе."



def test_gpu_share_fits_free_video_memory(brain, env, monkeypatch):
    """Gemma 26B (15,6 ГБ з модулем зору) на 16-гіговій карті, де ≈3 ГБ уже зайнято:
    на відеокарту лише стільки шарів, скільки влазить, з запасом, а не всі (падала)."""
    gpu_ratio = next(v for m, n, v in env._saved if n == "_gpu_ratio")     # conftest ставить заглушку
    monkeypatch.setattr(brain, "_model_size", lambda name: 15.63e9)
    monkeypatch.setattr(brain, "_free_vram", lambda: 13.9e9)
    assert 0.65 <= gpu_ratio("google/gemma-4-26b-a4b-qat") <= 0.75      # (13,9 - 1 запас - 1,5 контекст) / 15,6
    monkeypatch.setattr(brain, "_free_vram", lambda: 40e9)
    assert gpu_ratio("google/gemma-4-26b-a4b-qat") == 1.0                # велика карта: усе на відеокарту
    monkeypatch.setattr(brain, "_free_vram", lambda: None)
    assert gpu_ratio("google/gemma-4-26b-a4b-qat") == env.get("BRAIN_LOCAL_GPU_RATIO")



def test_cloud_chain_first_local_only_when_cloud_is_silent(env, brain):
    """2026-11-11: розмова в хмарі (3.5 Lite, 3.1 Lite, gpt-oss-120b і її запасна 20b), локальна
    остання: лише без інтернету. Мишкою (screen_do) вона керує й далі."""
    env.patch("GROQ_PRIMARY_MODEL", "gemini:gemini-3.5-flash-lite")
    env.patch("GROQ_FALLBACK_MODEL", "gemini:gemini-3.1-flash-lite")
    env.patch("CLOUD_RESERVE_MODEL", "openai/gpt-oss-120b")
    env.patch("GROQ_SPARE_MODEL", "openai/gpt-oss-20b")
    env.patch("BRAIN_LOCAL_TALK", False)
    assert brain.main_model() == "gemini:gemini-3.5-flash-lite"
    assert brain._candidates(brain.main_model(), None) == [
        "gemini:gemini-3.5-flash-lite", "gemini:gemini-3.1-flash-lite", "openai/gpt-oss-120b", "openai/gpt-oss-20b", LOCAL]
    assert brain._model_kwargs("gemini:gemini-3.5-flash-lite") == {"reasoning_effort": "low"}


def test_gemini_gets_room_to_think():
    """2026-11-15: на max_tokens=450 3.5 Flash Lite обірвала «Лови свій вай» без тегу дії."""
    from raphael import llm
    assert llm._room_to_think("gemini-3.5-flash-lite", {"max_tokens": 450})["max_tokens"] == llm.GEMINI_MIN_TOKENS
    assert llm._room_to_think("gemini-3.5-flash-lite", {"max_tokens": 4000})["max_tokens"] == 4000
    assert llm._room_to_think("openai/gpt-oss-120b", {"max_tokens": 450})["max_tokens"] == 450   # Groq: запас їсть ліміт