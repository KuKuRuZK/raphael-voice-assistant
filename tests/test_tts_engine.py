"""Двигун озвучки: локальний TTS-сервер, Piper, і edge-tts як запасний."""
import io
import json
import os

import pytest


@pytest.fixture
def tts(env, tmp_path):
    from raphael import tts as module
    env.patch("TTS_CACHE_DIR", str(tmp_path / "tts"))
    env.patch("_http_down_until", 0.0)
    env.patch("_edge_down_until", 0.0)
    env.patch("_tts_synth_to", lambda path, text: open(path, "wb").write(b"EDGE"))
    return module


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def test_http_engine_posts_openai_speech_request(tts, env):
    sent = []

    def urlopen(req, timeout):
        sent.append((req.full_url, json.loads(req.data), timeout))
        return io.BytesIO(b"RIFFwav")

    env.patch("TTS_ENGINE", "http")
    env.patch("TTS_HTTP_URL", "http://127.0.0.1:8880/v1/audio/speech")
    env.patch("TTS_HTTP_MODEL", "kokoro")
    env.patch("TTS_HTTP_VOICE", "")
    env._saved.append((tts.urllib.request, "urlopen", tts.urllib.request.urlopen))
    tts.urllib.request.urlopen = urlopen

    path, temp = tts._tts_audio("Привіт")
    try:
        assert temp and path.endswith(".wav") and _read(path) == b"RIFFwav"
    finally:
        os.unlink(path)
    url, body, _ = sent[0]
    assert url.endswith("/v1/audio/speech")
    assert body == {"input": "Привіт", "response_format": "wav", "model": "kokoro"}


def test_http_engine_down_falls_back_to_edge_and_waits(tts, env):
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise ConnectionRefusedError("refused")

    env.patch("TTS_ENGINE", "http")
    env._saved.append((tts.urllib.request, "urlopen", tts.urllib.request.urlopen))
    tts.urllib.request.urlopen = urlopen

    for _ in range(3):
        path, temp = tts._tts_audio("Слухаю.")
        assert _read(path) == b"EDGE"
    assert len(calls) == 1          # після збою сервер хвилину не чіпаємо


def test_edge_is_default(tts, env):
    env.patch("TTS_ENGINE", "edge")
    path, temp = tts._tts_audio("Слухаю.")
    assert _read(path) == b"EDGE" and not temp     # коротка фраза в кеші


def test_piper_engine(tts, env):
    env.patch("TTS_ENGINE", "piper")
    env.patch("_piper_ready", lambda: True)
    env.patch("_piper_audio", lambda text: "piper.wav")
    assert tts._tts_audio("Слухаю.") == ("piper.wav", True)


def test_tts_settings_from_config(lin, env, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"TTS_ENGINE": "http", "TTS_HTTP_VOICE": "uk_female"}),
                        encoding="utf-8")
    env.patch("CONFIG_PATH", str(cfg_path))
    env.patch("TTS_ENGINE", "edge")
    env.patch("TTS_HTTP_VOICE", "")
    env.get("_load_config")()
    assert env.get("TTS_ENGINE") == "http"
    assert env.get("TTS_HTTP_VOICE") == "uk_female"
