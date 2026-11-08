"""Новини зі стрічок (raphael/news.py): 2026-11-08 Лін на «дай новини» казала «не знаю» або вигадувала."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from raphael import news


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
                   f"<pubDate>{format_datetime(when)}</pubDate></item>" for t, link, d, when in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


@pytest.fixture
def feeds(monkeypatch):
    import httpx
    now = datetime.now(timezone.utc)
    pages = {
        "https://feeds.bbci.co.uk/ukrainian/rss.xml": rss(
            ("Стрілянина в центрі Кракова", "https://bbc.com/ua/1", "Шестеро загиблих", now - timedelta(hours=2))),
        "https://www.pravda.com.ua/rss/view_news/": rss(
            ("Генштаб: 199 боїв за добу", "https://pravda.com.ua/news/1", "", now - timedelta(hours=1)),
            ("Стара новина", "https://pravda.com.ua/news/0", "", now - timedelta(days=3))),
        "https://rss.unian.net/site/news_ukr.rss": rss(
            ("Як зберігати цвітну капусту", "https://www.unian.ua/curiosities/1", "", now - timedelta(minutes=5)),
            ("Енергетики відновили світло на Харківщині", "https://www.unian.ua/economics/2", "", now - timedelta(hours=3))),
        "https://feeds.bbci.co.uk/news/world/rss.xml": b"<html>not rss</html>",          # стрічка зламалась
    }

    class R:
        def __init__(self, content):
            self.content = content
    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(pages[url]))


def test_fresh_headlines_without_junk(feeds):
    titles = [n["title"] for n in news.headlines()]
    assert titles == ["Генштаб: 199 боїв за добу", "Стрілянина в центрі Кракова",
                      "Енергетики відновили світло на Харківщині"]          # капусти й старого нема, зламана стрічка не валить


def test_topic_survives_word_endings(feeds):
    assert [n["title"] for n in news.headlines("Краків")] == ["Стрілянина в центрі Кракова"]
    assert [n["source"] for n in news.headlines("Харків")] == ["УНІАН"]


def test_digest_has_time_source_and_link(feeds):
    line = news.digest(news.headlines("Краків"))
    assert "BBC Україна" in line and "https://bbc.com/ua/1" in line and "Шестеро загиблих" in line


def test_news_question_is_a_command_not_an_unasked_action(env, lin):
    """Gemma хотіла шукати, а перевірка відкидала це як «дію без прохання»."""
    from raphael import llm
    assert llm._is_command("Дай мені найважливіші новини світу і України за сьогодні")
    assert llm._find_action("[ACTION:news:]")[0] == "news"
    assert "news" in llm._LOCAL_TALK_ACTIONS


def test_home_retells_news(env, lin, monkeypatch):
    from raphael import actions, llm, tts, web
    monkeypatch.setattr(news, "headlines", lambda topic="": [{"when": datetime.now(timezone.utc), "source": "BBC",
                                                              "title": "Подія", "about": "", "link": "https://x"}])
    asked = []
    monkeypatch.setattr(llm, "llm_stream", lambda model, messages, **kw: asked.append(messages) or iter(["Ось новини."]))
    monkeypatch.setattr(tts, "speak_stream", lambda chunks: "".join(chunks))
    monkeypatch.setattr(web.threading, "Thread", lambda target, daemon=True: type("T", (), {"start": lambda s: target()})())
    actions.execute_action("[ACTION:news:]")
    assert "нічого не додавай" in asked[0][0]["content"] and "https://x" in asked[0][0]["content"]


def test_server_lin_uses_news_tool(tmp_path, monkeypatch):
    from cloud import brain as brain_mod
    from cloud import tools
    monkeypatch.setattr(tools, "news", lambda topic="": "10:00 | BBC | Подія | https://x\n\n" + news.RETELL)
    replies, seen = ["[ACTION:news:]", "Головне: подія (BBC)."], []
    monkeypatch.setattr(brain_mod, "chat", lambda messages, max_tokens=600: seen.append(messages) or replies.pop(0))
    b = brain_mod.Brain(tools.Reminders(str(tmp_path / "r.json")), str(tmp_path / "h.json"))
    assert b.answer("що нового в Україні?") == "Головне: подія (BBC)."
    assert "нічого не додавай" in seen[1][-1]["content"]
