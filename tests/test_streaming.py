"""Озвучка по реченнях і відповідь моделі потоком."""
import time
import types

import pytest


# ── Нарізка на речення ────────────────────────────────────────────────────────
def test_first_sentence_is_emitted_alone(env):
    out = list(env.get("_sentences")(["Ну. Друге речення коротке. Третє теж. "
                                      "А це вже довге речення, яке саме по собі перевищує поріг склеювання фраз."]))
    assert out[0] == "Ну."                                   # перше одразу, навіть коротке
    assert out[1] == "Друге речення коротке. Третє теж. А це вже довге речення, " \
                     "яке саме по собі перевищує поріг склеювання фраз."


def test_sentences_from_a_stream_of_fragments(env):
    fragments = ["При", "віт! Як ", "справи? Все доб", "ре"]
    assert list(env.get("_sentences")(fragments)) == ["Привіт!", "Як справи? Все добре"]


def test_code_blocks_are_not_read(env):
    out = " ".join(env.get("_sentences")(["Ось приклад: ```print(1)\nx = 2``` і все."]))
    assert "print" not in out and "x = 2" not in out
    assert out.startswith("Ось приклад:") and out.endswith("і все.")


# ── Синтез паралельно з програванням ──────────────────────────────────────────
@pytest.fixture
def audio(env, tmp_path):
    """Справжній speak_stream з фейковими синтезом (0.15 с) і програванням (0.3 с)."""
    real_speak = env._saved[0][2]
    env.patch("speak", real_speak)
    env.patch("TTS_CACHE_DIR", str(tmp_path / "tts"))
    env.patch("LIN_UI", None)
    events = []

    def synth(path, text):
        events.append(("synth", text, time.monotonic()))
        time.sleep(0.15)
        open(path, "wb").write(b"x")

    def play(path):
        events.append(("play+", path, time.monotonic()))
        time.sleep(0.3)
        events.append(("play-", path, time.monotonic()))

    env.patch("_tts_synth_to", synth)
    env.patch("_tts_play", play)
    return events


def test_next_sentence_is_synthesized_while_current_plays(env, audio):
    long = "Це довге речення номер {}, достатньо довге, щоб не склеюватись із сусідами."
    text = " ".join(long.format(i) for i in range(1, 4))
    spoken = env.get("speak_stream")([text])
    assert spoken.count("довге речення") == 3
    synth_2 = [t for kind, x, t in audio if kind == "synth"][1]
    play_1_end = [t for kind, x, t in audio if kind == "play-"][0]
    assert synth_2 < play_1_end            # друге синтезувалось, поки грало перше


def test_source_failing_before_any_text_raises(env, audio):
    def broken():
        raise ConnectionError("модель не відповіла")
        yield  # noqa: unreachable, робить функцію генератором

    with pytest.raises(ConnectionError):
        env.get("speak_stream")(broken())


def test_source_failing_midway_keeps_what_was_said(env, audio):
    def half():
        yield "Перше речення прозвучить. "
        raise ConnectionError("обрив")

    assert env.get("speak_stream")(half()) == "Перше речення прозвучить."
    assert env.get("_last_spoken") == "Перше речення прозвучить."


def test_microphone_state_is_released_after_speech(env, audio):
    env.get("speak_stream")(["Коротко."])
    assert not env.get("_tts_active").is_set()


# ── Модель потоком ────────────────────────────────────────────────────────────
def _chunk(content=None, reasoning=None):
    delta = types.SimpleNamespace(content=content, reasoning=reasoning)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, model, messages, stream=False, **kw):
        assert stream
        r = self.behaviour[model]
        if isinstance(r, Exception):
            raise r
        return iter(r)


@pytest.fixture
def models(env):
    def install(behaviour):
        client = FakeClient(behaviour)
        env.patch("_llm", lambda m: (client, m.split(":", 1)[-1]))
        env.patch("GROQ_PRIMARY_MODEL", "primary")
        env.patch("GROQ_FALLBACK_MODEL", "fallback")
    return install


def test_stream_skips_reasoning_and_service_chunks(env, models):
    models({"primary": [_chunk(reasoning="думаю..."), types.SimpleNamespace(choices=[]),
                        _chunk("При"), _chunk("віт.")]})
    assert "".join(env.get("llm_stream")("primary", [])) == "Привіт."


def test_stream_falls_back_before_first_text(env, models):
    models({"primary": TimeoutError("read timeout"), "fallback": [_chunk("Резерв.")]})
    assert "".join(env.get("llm_stream")("primary", [])) == "Резерв."


def test_stream_both_down(env, models):
    models({"primary": TimeoutError("a"), "fallback": ConnectionError("b")})
    with pytest.raises(env.get("LLMUnavailable")):
        list(env.get("llm_stream")("primary", []))


def test_mail_summary_falls_back_to_excerpt(env, models):
    models({"primary": TimeoutError("a"), "fallback": ConnectionError("b")})
    said = []
    env.patch("speak_stream", lambda chunks: said.append("".join(chunks)) or said[-1])
    long_body = "Текст листа. " * 80
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": ""}, "headers": [
        {"name": "From", "value": "Іван <ivan@example.com>"}, {"name": "Subject", "value": "Звіт"}]}}
    svc = types.SimpleNamespace(users=lambda: svc, messages=lambda: svc,
                                get=lambda **k: types.SimpleNamespace(execute=lambda: msg))
    env.patch("_gmail_accounts", lambda: [("основна", svc)])
    env.patch("_gmail_find", lambda q: ("основна", svc, "m1"))
    env.patch("_gmail_extract_body", lambda full: long_body)
    env.get("_gmail_read_full")("")
    assert said[0].startswith("Лист від Іван, тема «Звіт». Переказати не вийшло, ось початок: Текст листа.")
