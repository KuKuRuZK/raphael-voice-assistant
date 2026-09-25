"""Офлайн: локальна модель після хмарних і голос Piper, коли edge-tts недоступний."""
import os
import subprocess
import types

import pytest


def _reply(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=text))])


@pytest.fixture
def models(env):
    """Моделі з заданою поведінкою: текст відповіді або виняток."""
    def install(behaviour, offline="local:gemma3:4b"):
        def create(model, messages, stream=False, **kw):
            r = behaviour[model]
            if isinstance(r, Exception):
                raise r
            if stream:
                delta = types.SimpleNamespace(content=r, reasoning=None)
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])])
            return _reply(r)
        client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        env.patch("_llm", lambda m: (client, m))
        env.patch("GROQ_PRIMARY_MODEL", "primary")
        env.patch("GROQ_FALLBACK_MODEL", "gemini:fallback")
        env.patch("OFFLINE_MODEL", offline)
    return install


def test_candidates(env, models):
    models({})
    candidates = env.get("_candidates")
    assert candidates("primary", None) == ["primary", "gemini:fallback", "local:gemma3:4b"]
    assert candidates("gemini:vision", False) == ["gemini:vision"]      # зір без резервів
    assert candidates("local:gemma3:4b", None) == ["local:gemma3:4b", "primary"]


def test_local_model_answers_without_internet(env, models):
    models({"primary": ConnectionError("no internet"), "gemini:fallback": ConnectionError("no internet"),
            "local:gemma3:4b": "Локально."})
    assert env.get("llm_chat")("primary", []).choices[0].message.content == "Локально."
    assert "".join(env.get("llm_stream")("primary", [])) == "Локально."


def test_without_local_model_nothing_changes(env, models):
    models({"primary": ConnectionError("a"), "gemini:fallback": ConnectionError("b")}, offline="")
    with pytest.raises(env.get("LLMUnavailable")) as err:
        env.get("llm_chat")("primary", [])
    assert [m for m, _ in err.value.errors] == ["primary", "gemini:fallback"]


def test_limit_is_still_reported_when_ollama_is_off(env, models):
    models({"primary": RuntimeError("Error code: 429 rate_limit_exceeded"),
            "gemini:fallback": RuntimeError("429 quota exceeded"),
            "local:gemma3:4b": ConnectionError("Connection refused")})
    with pytest.raises(env.get("LLMUnavailable")) as err:
        env.get("llm_chat")("primary", [])
    assert err.value.rate_limited


# ── Голос ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def voice(env, tmp_path):
    calls = {"edge": 0, "piper": 0}

    def edge_down(path, text):
        calls["edge"] += 1
        raise OSError("Cannot connect to host speech.platform.bing.com")

    def piper(text):
        calls["piper"] += 1
        return str(tmp_path / f"piper{calls['piper']}.wav")

    env.patch("TTS_CACHE_DIR", str(tmp_path / "cache"))
    env.patch("_tts_synth_to", edge_down)
    env.patch("_piper_ready", lambda: True)
    env.patch("_piper_audio", piper)
    env.patch("_edge_down_until", 0.0)
    return calls


def test_piper_speaks_when_edge_is_down(env, voice):
    audio = env.get("_tts_audio")
    path, temporary = audio("Інтернету немає.")
    assert path.endswith("piper1.wav") and temporary
    audio("Друге речення.")
    assert voice == {"edge": 1, "piper": 2}         # edge хвилину не смикаємо


def test_no_piper_keeps_old_behaviour(env, voice):
    env.patch("_piper_ready", lambda: False)
    with pytest.raises(OSError):
        env.get("_tts_audio")("Інтернету немає.")


def test_text_without_words_is_not_a_network_problem(env, voice):
    class NoAudioReceived(Exception):
        pass

    def no_audio(path, text):
        raise NoAudioReceived("No audio was received")
    env.patch("_tts_synth_to", no_audio)
    with pytest.raises(NoAudioReceived):
        env.get("_tts_audio")("...")
    assert voice["piper"] == 0


def test_piper_command(env, monkeypatch, tmp_path):
    seen = {}

    def run(cmd, input=None, **kw):
        seen["cmd"], seen["input"] = cmd, input
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    env.patch("PIPER_EXE", "C:/raphael/piper/piper.exe")
    env.patch("PIPER_MODEL", "C:/raphael/piper/uk.onnx")
    path = env.get("_piper_audio")("Привіт")
    assert seen["cmd"] == ["C:/raphael/piper/piper.exe", "-m", "C:/raphael/piper/uk.onnx", "-f", path]
    assert seen["input"] == "Привіт".encode("utf-8")
    os.unlink(path)
