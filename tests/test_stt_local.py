"""Локальний Whisper перший у черзі (2026-11-15: Groq вичерпав 2000 запитів за день)."""
import speech_recognition as sr

from raphael import stt, stt_local


def audio():
    return sr.AudioData(bytes(32000), 16000, 2)       # 1 с тиші


def chain(monkeypatch, local):
    called = []
    monkeypatch.setattr(stt_local, "available", lambda: local is not False)
    monkeypatch.setattr(stt_local, "transcribe", lambda s, p="": local)
    monkeypatch.setattr(stt, "_transcribe_whisper", lambda a: called.append("groq") or "з groq")
    monkeypatch.setattr(stt, "_transcribe_google", lambda a: called.append("google") or "")
    monkeypatch.setattr(stt, "_transcribe_vosk", lambda a: called.append("vosk") or "")
    return called


def test_local_text_wins_and_groq_is_not_asked(monkeypatch):
    called = chain(monkeypatch, "Лін, увімкни музику")
    assert stt._transcribe(audio()) == "лін, увімкни музику" and called == []


def test_local_noise_does_not_go_to_groq(monkeypatch):
    """Шум кімнати локально розпізнано як «дякую за перегляд»: це тиша, Groq не питаємо."""
    called = chain(monkeypatch, "Дякую за перегляд")
    assert stt._transcribe(audio()) == "" and called == []


def test_local_failed_falls_back_to_groq(monkeypatch):
    called = chain(monkeypatch, None)
    assert stt._transcribe(audio()) == "з groq" and called == ["groq"]


def test_no_local_model_uses_groq(monkeypatch):
    called = chain(monkeypatch, False)
    assert stt._transcribe(audio()) == "з groq" and called == ["groq"]


def test_prompt_echo_is_stripped():
    assert stt_local._strip_prompt("Лін, Spotify. Увімкни музику", "Лін, Spotify.") == "Увімкни музику"


def test_off_switch_never_loads(monkeypatch):
    from raphael import settings as cfg
    monkeypatch.setattr(cfg, "STT_LOCAL", False)
    monkeypatch.setitem(stt_local._whisper, "model", None)
    monkeypatch.setitem(stt_local._whisper, "failed", False)
    assert stt_local.load() is False
