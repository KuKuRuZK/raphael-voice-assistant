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
    from raphael import tts
    env.patch("TTS_HTTP_URL", "http://127.0.0.1:9/v1/audio/speech")       # там нікого
    assert tts.http_server_up(timeout=0.3) is False
