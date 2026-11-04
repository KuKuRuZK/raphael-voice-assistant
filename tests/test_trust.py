"""Довіра: росте від спільного часу й теплих слів, падає від грубих і коли Влада нема."""
from datetime import datetime

import pytest


@pytest.fixture
def trust(env):
    from raphael import trust
    return trust


def _value():
    from raphael import notes
    return notes.load_memory()["trust"]["value"]


def test_starts_where_persona_is(trust):
    assert trust.level(datetime(2026, 11, 3, 9, 0)) == trust.START
    assert trust.prompt_line(datetime(2026, 11, 3, 9, 0)) == ""            # persona.txt і так про це


def test_talk_and_kind_words_grow_it(trust):
    now = datetime(2026, 11, 3, 9, 0)
    trust.on_exchange("котра година", now)
    assert _value() == pytest.approx(trust.START + trust.PER_EXCHANGE)
    trust.on_exchange("дякую, ти молодець", now)
    assert _value() == pytest.approx(trust.START + 2 * trust.PER_EXCHANGE + trust.KIND_BONUS)


def test_daily_gain_is_capped(trust):
    now = datetime(2026, 11, 3, 9, 0)
    for _ in range(50):
        trust.on_exchange("дякую", now)
    assert _value() == pytest.approx(trust.START + trust.DAILY_GAIN_CAP)
    trust.on_exchange("дякую", datetime(2026, 11, 4, 9, 0))          # новий день, нова межа
    assert _value() > trust.START + trust.DAILY_GAIN_CAP


def test_rude_words_cost_even_after_the_cap(trust):
    now = datetime(2026, 11, 3, 9, 0)
    for _ in range(50):
        trust.on_exchange("ага", now)
    capped = _value()
    trust.on_exchange("заткнись, дура", now)
    assert _value() == pytest.approx(capped - trust.RUDE_PENALTY)


def test_days_away_cool_her_down(trust):
    trust.on_exchange("привіт", datetime(2026, 11, 3, 9, 0))
    start = _value()
    assert trust.level(datetime(2026, 11, 4, 9, 0)) == start               # один день це нормально
    assert trust.level(datetime(2026, 11, 13, 9, 0)) == pytest.approx(start - 9 * trust.DECAY_PER_DAY)
    trust.on_exchange("я повернувся", datetime(2026, 11, 13, 9, 0))
    assert _value() == pytest.approx(start - 9 * trust.DECAY_PER_DAY + trust.PER_EXCHANGE)


@pytest.mark.parametrize("value,words", [(10, "ображена"), (40, "насторожі"), (80, "довіряєш"), (95, "дуже близькі")])
def test_prompt_line_follows_level(trust, value, words):
    from raphael import notes
    notes.save_memory({"trust": {"value": value, "day": "2026-11-03", "gained_today": 0,
                                 "last_seen": "2026-11-03T09:00"}})
    assert words in trust.prompt_line(datetime(2026, 11, 3, 10, 0))


def test_reply_updates_trust_and_prompt_has_it(trust, env):
    from conftest import fake_reply
    from raphael import assistant, prompt
    env.patch("_llm_call", lambda model, messages, **kw: fake_reply("[EMO:happy] Та нема за що."))
    assistant.ask_lin("дякую тобі")
    assert _value() > trust.START
    from raphael import notes
    notes.save_memory({"trust": {"value": 80, "day": datetime.now().date().isoformat(), "gained_today": 0,
                                 "last_seen": datetime.now().isoformat(timespec="minutes")}})
    assert "ДОВІРА: ти йому довіряєш" in prompt.build_system_prompt()


@pytest.mark.parametrize("text,delta", [
    # шматки інших слів не рахуються
    ("постав процедуру на завтра", "plain"),
    ("я зараз поступаю в магістратуру", "plain"),
    ("куди класти ключі?", "plain"),
    ("суперечка вийшла", "plain"),
    # цілі слова рахуються, і з розділовими знаками
    ("Дякую!", "kind"),
    ("ну класно вийшло", "kind"),
    ("ти найкраща, Лін", "kind"),
    ("ти дура, чи що?", "rude"),
    ("заткнися вже", "rude"),
])
def test_only_whole_words_count(trust, text, delta):
    trust.on_exchange(text, datetime(2026, 11, 4, 9, 0))
    expected = {"plain": trust.PER_EXCHANGE, "kind": trust.PER_EXCHANGE + trust.KIND_BONUS,
                "rude": trust.PER_EXCHANGE - trust.RUDE_PENALTY}[delta]
    assert _value() == pytest.approx(trust.START + expected)
