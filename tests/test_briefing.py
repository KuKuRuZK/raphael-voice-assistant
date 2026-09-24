"""Ранковий дайджест і тап посеред мови."""
import sys
import time
import types
from datetime import datetime

import pytest


@pytest.fixture
def slow_parts(env):
    """Кожна частина дайджесту «ходить у мережу» 0.3 с."""
    def slow(text):
        def fetch(*a, **k):
            time.sleep(0.3)
            return text
        return fetch
    env.patch("_get_weather", slow("ясно"))
    env.patch("_get_calendar", lambda: object())
    env.patch("_calendar_agenda_text", slow("Сьогодні подій немає."))
    env.patch("_gmail_accounts", lambda: [("основна", None)])
    env.patch("_gmail_unread_text", slow("Непрочитаних листів немає."))
    env.patch("_system_health_report", slow("Все в нормі."))
    env.patch("_auth_dead", {})


def test_briefing_parts_load_in_parallel(env, slow_parts):
    started = time.monotonic()
    env.get("morning_briefing")()
    assert time.monotonic() - started < 0.9          # по черзі було б 1.2 с
    assert env.spoken[1:] == ["Погода: ясно", "Сьогодні подій немає.",
                              "Непрочитаних листів немає.", "Все в нормі.", "Гарного дня!"]


def test_briefing_names_dead_google_access_once(env, slow_parts):
    env.patch("_get_calendar", lambda: None)
    env.patch("_gmail_accounts", lambda: [])
    env.get("_auth_dead")[env.get("GMAIL_TOKEN_PATH")] = "invalid_grant"
    env.get("morning_briefing")()
    assert sum("Немає доступу" in s for s in env.spoken) == 1


def test_briefing_is_not_repeated_after_restart(env, monkeypatch):
    class Stop(Exception):
        pass

    def stop(seconds):
        raise Stop
    briefing = sys.modules["raphael.briefing"]
    monkeypatch.setattr(briefing, "time", types.SimpleNamespace(sleep=stop))
    env.patch("BRIEFING_HOUR", datetime.now().hour)
    calls = []
    env.patch("morning_briefing", lambda: calls.append(1))
    for _ in range(2):                                # два запуски Рафаеля поспіль
        with pytest.raises(Stop):
            env.get("briefing_loop")()
    assert calls == [1]
    assert env.get("load_memory")()["last_briefing"] == datetime.now().strftime("%Y-%m-%d")


def test_tap_during_speech_only_stops_it(env):
    tts = sys.modules["raphael.tts"]
    hotkeys = sys.modules["raphael.hotkeys"]
    key = env.get("TAP_TOGGLE_KEY")
    tts._tts_active.set()
    try:
        hotkeys._tap_watcher(types.SimpleNamespace(name=key, event_type="down"))
        hotkeys._tap_watcher(types.SimpleNamespace(name=key, event_type="up"))
        assert tts._tts_stop.is_set()
        assert not hotkeys._ptt_event.is_set()           # не слухає команду
    finally:
        tts._tts_active.clear()
        tts._tts_stop.clear()
        hotkeys._ptt_event.clear()
