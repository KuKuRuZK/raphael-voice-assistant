"""Щоденник розмов: памʼять між запусками, підсумки днів, пошук «що я казав»."""
import json
from datetime import date, datetime

import pytest

from conftest import fake_reply


@pytest.fixture
def diary(env):
    from raphael import diary
    return diary


def test_exchange_is_written_without_tags(diary, env):
    diary.add("лін, постав паузу", "[EMO:smug] Ставлю. [ACTION:spotify_pause:]", datetime(2026, 11, 2, 23, 5))
    assert diary.read_day("2026-11-02") == [{"t": "23:05", "user": "лін, постав паузу", "lin": "Ставлю."}]


def test_broken_line_does_not_lose_the_day(diary, env):
    import os
    diary.add("раз", "один", datetime(2026, 11, 2, 10, 0))
    with open(os.path.join(env.get("DIARY_DIR"), "2026-11-02.jsonl"), "a", encoding="utf-8") as f:
        f.write("{зламано\n")
    diary.add("два", "другий", datetime(2026, 11, 2, 10, 1))
    assert [r["user"] for r in diary.read_day("2026-11-02")] == ["раз", "два"]


def test_recent_turns_survive_restart_even_over_midnight(diary):
    diary.add("стара розмова", "давня", datetime(2026, 11, 2, 18, 0))
    diary.add("до півночі", "ага", datetime(2026, 11, 2, 23, 50))
    diary.add("після півночі", "угу", datetime(2026, 11, 3, 0, 20))
    turns = diary.recent_turns(datetime(2026, 11, 3, 0, 30))
    assert [m["content"] for m in turns] == ["до півночі", "ага", "після півночі", "угу"]
    assert diary.recent_turns(datetime(2026, 11, 3, 9, 0)) == []        # давно: починаємо з чистого


def test_summaries_for_past_days_go_to_memory_and_prompt(diary, env):
    from raphael import notes
    for minute in range(3):
        diary.add("лін, нагадай про ARK о дев'ятій", "Нагадаю.", datetime(2026, 11, 1, 20, minute))
    diary.add("сьогоднішнє", "не підсумовуємо", datetime(2026, 11, 3, 9, 0))
    asked = []

    def chat(model, messages, **kw):
        asked.append(messages[0]["content"])
        return fake_reply("Влад грав в ARK і просив нагадати про рейд.")
    env.patch("llm_chat", chat)
    assert diary.summarize_pending(date(2026, 11, 3)) == 1
    assert "ARK" in asked[0] and "сьогоднішнє" not in asked[0]
    assert notes.load_memory()["days"] == {"2026-11-01": "Влад грав в ARK і просив нагадати про рейд."}
    assert diary.summarize_pending(date(2026, 11, 3)) == 0                  # двічі не пише
    block = diary.prompt_block(date(2026, 11, 3))
    assert block.startswith("СПОГАДИ") and "позавчора: Влад грав в ARK" in block


def test_short_day_is_summed_up_without_model(diary, env):
    from raphael import notes
    diary.add("привіт", "Привіт.", datetime(2026, 11, 1, 20, 0))
    env.patch("llm_chat", lambda *a, **k: pytest.fail("кілька реплік модель не підсумовує"))
    assert diary.summarize_pending(date(2026, 11, 3)) == 1
    assert notes.load_memory()["days"]["2026-11-01"] == "Коротко: «привіт»."


def test_trim_cuts_at_sentence_end(diary):
    assert diary._trim("Перше речення. Друге довге речення тут.", 30) == "Перше речення."
    assert diary._trim("Одне дуже довге речення без крапки", 20) == "Одне дуже довге…"


def test_silent_model_leaves_day_for_later(diary, env):
    from raphael import notes
    for minute in range(3):
        diary.add("щось", "щось", datetime(2026, 11, 2, 20, minute))
    env.patch("llm_chat", lambda *a, **k: fake_reply(""))
    assert diary.summarize_pending(date(2026, 11, 3)) == 0
    assert not notes.load_memory().get("days")


def test_prompt_uses_diary_instead_of_last_session(diary, env):
    from raphael import notes, prompt
    notes.save_memory({"last_session": "Стара сесія.", "days": {"2026-11-02": "Вчора слухали Linkin Park."}})
    env.patch("prompt_block", lambda today=None: "СПОГАДИ: вчора: Вчора слухали Linkin Park. ")
    p = prompt.build_system_prompt()
    assert "Linkin Park" in p and "Стара сесія" not in p


def test_find_by_words_and_by_day(diary):
    diary.add("ми сьогодні рейдимо базу в арку", "Удачі з рейдом.", datetime(2026, 10, 30, 21, 0))
    diary.add("постав таймер", "Поставила.", datetime(2026, 11, 2, 12, 0))
    diary.add("купив нову відеокарту", "О, яку?", datetime(2026, 11, 2, 13, 0))
    today = date(2026, 11, 3)
    assert [r["user"] for _, r in diary.find("що я казав про рейд", today=today)] == ["ми сьогодні рейдимо базу в арку"]
    assert [r["user"] for _, r in diary.find("про що ми говорили вчора", today=today)] == \
        ["постав таймер", "купив нову відеокарту"]
    assert [r["user"] for _, r in diary.find("що я вчора казав про відеокарту", today=today)] == \
        ["купив нову відеокарту"]


def test_reply_is_written_to_diary(diary, env):
    from raphael import assistant
    env.patch("_llm_call", lambda model, messages, **kw: fake_reply("[EMO:happy] Привіт, Владе!"))
    assistant.ask_lin("привіт")
    rows = diary.read_day(datetime.now().strftime("%Y-%m-%d"))
    assert rows[-1]["user"] == "привіт" and rows[-1]["lin"] == "Привіт, Владе!"


def test_diary_find_is_an_action(lin):
    from raphael import llm
    assert llm._find_action("[EMO:sly] Зараз згадаю. [ACTION:diary_find:що казав про ARK]")[:2] == \
        ("diary_find", "що казав про ARK")
