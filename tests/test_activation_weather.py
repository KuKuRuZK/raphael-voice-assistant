"""Клавіша активації поза режимом кнопки, «Ліно» як імʼя, погода з резервом."""
import sys
import types

import pytest


@pytest.fixture
def hk(env, monkeypatch):
    hotkeys = sys.modules["raphael.hotkeys"]
    beeps = []
    monkeypatch.setattr(hotkeys, "beep", lambda: beeps.append(1))
    hotkeys._ptt_event.clear()
    yield hotkeys, beeps
    hotkeys._ptt_event.clear()


def test_tap_in_normal_mode_is_taken_once(hk, env):
    hotkeys, beeps = hk
    env.patch("PUSH_TO_TALK", False)
    hotkeys.activate()
    assert hotkeys.take_activation() and not hotkeys.take_activation()


def test_stale_tap_is_forgotten(hk, env, monkeypatch):
    hotkeys, _ = hk
    env.patch("PUSH_TO_TALK", False)
    hotkeys.activate()
    monkeypatch.setattr(hotkeys, "_ptt_at", hotkeys._ptt_at - hotkeys.ACTIVATION_TTL - 1)
    assert not hotkeys.take_activation()


def test_command_after_tap_keeps_text_without_name(lin):
    from raphael import assistant
    assert assistant._without_name("лін, увімкни музику") == "увімкни музику"
    assert assistant._without_name("увімкни музику") == "увімкни музику"


@pytest.mark.parametrize("text", ["ліно, що в пошті", "лін, котра година"])
def test_vocative_name_wakes(lin, env, text):
    from raphael import voice_rules as vr
    assert vr.find_wake(text, env.get("WAKE_WORDS"))


def test_weather_falls_back_to_open_meteo(env):
    from raphael import web
    def refused(city):
        raise ConnectionRefusedError("[WinError 10061] No connection could be made")
    env.patch("_weather_wttr", refused)
    env.patch("_weather_open_meteo", lambda city: "Хмарно, 7°.")
    assert web._get_weather("Vilnius") == "Хмарно, 7°."


def test_weather_says_honestly_when_both_fail(env):
    from raphael import web
    def down(city):
        raise TimeoutError("timed out")
    env.patch("_weather_wttr", down)
    env.patch("_weather_open_meteo", down)
    assert "обидва" in web._get_weather("Vilnius")
