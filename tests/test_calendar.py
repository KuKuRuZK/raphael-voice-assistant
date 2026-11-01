"""Календар: час подій у твоєму поясі і нагадування за кілька хвилин до початку."""
import types
from datetime import datetime, timedelta, timezone

import pytest


def _ev(ev_id, start, summary, **extra):
    """Подія у форматі Google: start як datetime (з поясом) або date-рядок."""
    when = {"date": start} if isinstance(start, str) else {"dateTime": start.isoformat()}
    return {"id": ev_id, "start": when, "summary": summary, **extra}


class FakeCalendar:
    def __init__(self, items):
        self.items = items
        self.queries = []

    def events(self):
        return self

    def list(self, **kw):
        self.queries.append(kw)
        return types.SimpleNamespace(execute=lambda: {"items": self.items})


@pytest.fixture
def calendar(env):
    def install(items):
        svc = FakeCalendar(items)
        env.patch("_get_calendar", lambda: svc)
        return svc
    return install


def test_event_time_is_local(env):
    start = env.get("_event_start")
    utc_noon = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    local = utc_noon.astimezone().replace(tzinfo=None)
    assert start({"start": {"dateTime": "2026-09-24T12:00:00Z"}}) == local
    assert start({"start": {"dateTime": utc_noon.astimezone(timezone(timedelta(hours=9))).isoformat()}}) == local
    assert start({"start": {"date": "2026-09-24"}}) is None          # на весь день


def test_reminder_once_and_only_within_window(env):
    now = datetime(2026, 9, 24, 13, 50, 10)
    events = [("a", datetime(2026, 9, 24, 14, 0), "Стендап"),
              ("b", datetime(2026, 9, 24, 14, 30), "Обід"),
              ("c", datetime(2026, 9, 24, 13, 40), "Вже було")]
    announced = set()
    due = env.get("_due_reminders")
    assert due(events, now, announced, 10) == ["Нагадую: через 10 хвилин «Стендап»."]
    assert due(events, now + timedelta(seconds=30), announced, 10) == []      # не повторює
    assert due(events, datetime(2026, 9, 24, 14, 29), announced, 10) == ["Нагадую: через 1 хвилину «Обід»."]


def test_upcoming_skips_all_day_and_declined(env, calendar):
    soon = datetime.now().astimezone() + timedelta(minutes=20)
    calendar([
        _ev("1", soon, "Дзвінок"),
        _ev("2", datetime.now().strftime("%Y-%m-%d"), "День народження"),
        _ev("3", soon, "Чужа нарада", attendees=[{"self": True, "responseStatus": "declined"}]),
    ])
    assert [(i, s) for i, _, s in env.get("_upcoming_events")()] == [("1", "Дзвінок")]


def test_unavailable_calendar_keeps_quiet_without_reason(env):
    env.patch("_get_calendar", lambda: None)
    assert env.get("_upcoming_events")() is None


def test_week_agenda_names_the_day(env, calendar):
    thursday = datetime(2026, 9, 24, 10, 0).astimezone()
    calendar([_ev("1", thursday, "Співбесіда"), _ev("2", "2026-09-26", "Похід")])
    text = env.get("_calendar_agenda_text")("week")
    assert "четвер 10:00 Співбесіда" in text and "субота Похід" in text
