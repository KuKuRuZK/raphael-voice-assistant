"""Свій голос: Лін сама запускає локальний TTS-сервер, якщо він не відповідає."""
import sys

import pytest


@pytest.fixture
def popen(env, monkeypatch):
    from raphael import tts
    started = []
    monkeypatch.setattr(tts.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    env.patch("TTS_ENGINE", "http")
    env.patch("TTS_HTTP_SERVER", [sys.executable, "server.py"])
    return started


def test_starts_server_when_it_is_down(env, popen, monkeypatch):
    from raphael import tts
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: False)
    assert tts.start_http_server() is True
    assert popen == [[sys.executable, "server.py"]]


def test_leaves_running_server_alone(env, popen, monkeypatch):
    from raphael import tts
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: True)
    assert tts.start_http_server() is False and popen == []


@pytest.mark.parametrize("engine,cmd", [("edge", [sys.executable, "server.py"]), ("http", [])])
def test_nothing_to_start(env, popen, monkeypatch, engine, cmd):
    from raphael import tts
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: False)
    env.patch("TTS_ENGINE", engine)
    env.patch("TTS_HTTP_SERVER", cmd)
    assert tts.start_http_server() is False and popen == []


def test_server_up_checks_the_port(env):
    """Свій порт, який тест сам відкриває й закриває, а не «там нікого» на 127.0.0.1:9.
    (2026-11-08 тест падав не через порт: автофікстура в conftest повертала заглушку
    http_server_up замість справжньої, див. cloud_data.)"""
    import socket
    real = next(v for m, n, v in env._saved if n == "http_server_up")      # conftest його підміняє
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    env.patch("TTS_HTTP_URL", f"http://127.0.0.1:{srv.getsockname()[1]}/v1/audio/speech")
    assert real(timeout=1.0) is True
    srv.close()
    assert real(timeout=0.3) is False


def test_no_second_server_while_first_loads(env, popen, monkeypatch):
    from raphael import tts
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: False)
    monkeypatch.setattr(tts, "_server_started", 0.0)
    assert tts.start_http_server() is True
    assert tts.start_http_server() is False                    # ще вантажить модель
    assert len(popen) == 1


def test_speech_waits_for_own_voice_instead_of_old_one(env, popen, monkeypatch):
    """Раніше привітання на старті звучало старим голосом edge, бо свій ще вантажився."""
    import io
    from raphael import tts
    up = iter([False, False, False, True, True, True])
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: next(up))
    monkeypatch.setattr(tts, "_server_started", 0.0)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    env.patch("TTS_HTTP_URL", "http://127.0.0.1:8880/v1/audio/speech")
    env.patch("_http_down_until", 0.0)
    env._saved.append((tts.urllib.request, "urlopen", tts.urllib.request.urlopen))
    tts.urllib.request.urlopen = lambda req, timeout: io.BytesIO(b"RIFFwav")
    path = tts._engine_audio("Доброго ранку.")
    assert path and path.endswith(".wav") and len(popen) == 1


def test_old_voice_only_when_own_never_comes(env, popen, monkeypatch):
    from raphael import tts
    monkeypatch.setattr(tts, "http_server_up", lambda timeout=1.0: False)
    monkeypatch.setattr(tts, "_server_started", 0.0)
    monkeypatch.setattr(tts, "VOICE_WAIT", 0.01)
    env.patch("TTS_HTTP_URL", "http://127.0.0.1:8880/v1/audio/speech")
    env.patch("_http_down_until", 0.0)
    assert tts._engine_audio("Привіт.") is None                 # далі edge
    assert tts._http_down_until > tts.time.monotonic() + 500   # і довго не чекаємо знову
