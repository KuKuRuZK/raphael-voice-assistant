"""
Локальний детектор імені (WAKE_ENGINE = "vosk") на фейковому Vosk.

Точність розпізнавання тут не перевіряється (для цього потрібна справжня
модель і голос), лише логіка: що йде в Whisper, а що ні, і як детектор
поводиться, поки говорить сам Рафаель.
"""
import json
import sys
import types

import pytest

CHUNK_BYTES = 4000 * 2


class FakeRecognizer:
    """Кожен AcceptWaveform бере наступний крок сценарію:
    None (фраза триває), ("partial", текст) або ("final", текст)."""
    script = []
    instances = []

    def __init__(self, model, rate, grammar=None):
        self.grammar = json.loads(grammar) if grammar else None
        self.resets = 0
        self._step = None
        FakeRecognizer.instances.append(self)

    def AcceptWaveform(self, data):
        self._step = FakeRecognizer.script.pop(0) if FakeRecognizer.script else None
        return bool(self._step and self._step[0] == "final")

    def Result(self):
        return json.dumps({"text": self._step[1]})

    def PartialResult(self):
        partial = self._step[1] if self._step and self._step[0] == "partial" else ""
        return json.dumps({"partial": partial})

    def Reset(self):
        self.resets += 1


class FakeModel:
    def __init__(self, vocab):
        self.vocab = vocab

    def find_word(self, word):
        return 1 if word in self.vocab else -1


@pytest.fixture
def wake(env, monkeypatch):
    """Фейковий Vosk і мікрофон. Повертає лічильник прочитаних шматків."""
    monkeypatch.setitem(sys.modules, "vosk", types.SimpleNamespace(KaldiRecognizer=FakeRecognizer))
    FakeRecognizer.script, FakeRecognizer.instances = [], []
    env.patch("_get_vosk_model", lambda: FakeModel({"рафаель", "рафа", "привіт"}))
    env.patch("_wake_vocab", None)
    env.patch("TTS_ECHO_TAIL", 0.0)
    reads = {"n": 0, "on_read": None}

    class Stream:
        def read(self, size):
            reads["n"] += 1
            if reads["on_read"]:
                reads["on_read"](reads["n"])
            return b"\x01\x00" * size

    class Mic:
        def __init__(self, sample_rate=None, chunk_size=None):
            assert sample_rate == 16000
            self.stream = Stream()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    sr = env.get("sr")
    monkeypatch.setattr(sr, "Microphone", Mic)
    heard = []
    env.patch("_transcribe", lambda audio: heard.append(audio.get_raw_data()) or "рафа котра година")
    reads["heard"] = heard
    return reads


def test_only_phrase_with_name_goes_to_whisper(env, wake):
    FakeRecognizer.script = [("final", "[unk]"),                         # чужа фраза
                             None, ("partial", "рафа"), ("final", "рафа [unk]")]
    assert env.get("wait_for_wake")(timeout=5) == "рафа котра година"
    assert len(wake["heard"]) == 1
    assert len(wake["heard"][0]) == 3 * CHUNK_BYTES                     # лише друга фраза


def test_grammar_holds_only_names_from_model_vocabulary(env, wake):
    FakeRecognizer.script = [("final", "рафаель")]
    env.get("wait_for_wake")(timeout=5)
    grammar = FakeRecognizer.instances[0].grammar
    assert "[unk]" in grammar and "рафаель" in grammar and "рафа" in grammar
    assert "лін" not in grammar                                        # немає в словнику


def test_own_speech_restarts_the_phrase(env, wake):
    active = env.get("_tts_active")

    def during_speech(n):
        if n == 2:
            active.set()
        elif n == 4:
            active.clear()

    wake["on_read"] = during_speech
    FakeRecognizer.script = [None, None, ("final", "рафа")]
    try:
        env.get("wait_for_wake")(timeout=5)
    finally:
        active.clear()
    assert FakeRecognizer.instances[0].resets >= 1
    # шматок 1 до озвучки викинуто, 2-3 під час неї не слухали: у Whisper лише 4-5
    assert len(wake["heard"][0]) == 2 * CHUNK_BYTES


def test_no_names_in_vocabulary_falls_back(env, wake):
    env.patch("_get_vosk_model", lambda: FakeModel({"привіт"}))
    assert env.get("wait_for_wake")(timeout=5) is None
    assert env.get("wait_for_wake")(timeout=5) is None                 # кеш, без повторних спроб


def test_no_model_falls_back(env, wake):
    env.patch("_get_vosk_model", lambda: None)
    assert env.get("wait_for_wake")(timeout=5) is None


def test_interrupt_and_timeout(env, wake):
    assert env.get("wait_for_wake")(timeout=5, interrupt=lambda: True) == ""
    assert env.get("wait_for_wake")(timeout=0.05) == ""                # фраз з імʼям не було
    assert wake["heard"] == []


def test_microphone_error_falls_back(env, wake, monkeypatch):
    def broken(**kw):
        raise OSError("Invalid sample rate")
    monkeypatch.setattr(env.get("sr"), "Microphone", broken)
    env.patch("time", types.SimpleNamespace(monotonic=__import__("time").monotonic, sleep=lambda s: None))
    assert env.get("wait_for_wake")(timeout=5) is None
