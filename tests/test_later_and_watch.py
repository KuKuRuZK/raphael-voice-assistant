"""Відкладені дії (later:час|дія) і очікування листа (mail_watch)."""
import types
from datetime import datetime, timedelta

import pytest

from conftest import fake_reply


@pytest.fixture
def notes(env):
    from raphael import notes
    return notes


def test_later_stores_the_action(notes, env):
    said = notes.later("30|gmail_unread:")
    n = notes._load_notes()[-1]
    assert n["action"] == "gmail_unread:" and n["text"] == "перевіряю пошту"
    assert said.startswith("Добре, о ") and "перевіряю пошту" in said


def test_later_accepts_bracket_form(notes):
    notes.later("18:00|[ACTION:calendar_tomorrow:]")
    assert notes._load_notes()[-1]["action"] == "calendar_tomorrow:"


@pytest.mark.parametrize("bad", ["30|system_shutdown_pc:", "30|kill_process:chrome", "30|note_clear:", "30|type_text:привіт"])
def test_later_refuses_irreversible(notes, bad):
    assert "наперед не ставлю" in notes.later(bad)
    assert notes._load_notes() == []


def test_later_needs_time_and_action(notes):
    assert "Не зрозуміла" in notes.later("gmail_unread:")
    assert "Не зрозуміла, коли" in notes.later("коли-небудь|gmail_unread:")


def test_due_action_runs_instead_of_being_read(notes, env, monkeypatch):
    from raphael import actions
    ran = []
    monkeypatch.setattr(actions, "execute_action", ran.append)
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    notes._save_notes([{"id": 1, "text": "перевіряю пошту", "remind_at": past, "done": False, "action": "gmail_unread:"},
                       {"id": 2, "text": "подзвонити мамі", "remind_at": past, "done": False}])
    monkeypatch.setattr(notes.time, "sleep", lambda s: (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):
        notes.reminder_loop()
    assert ran == ["gmail_unread:"]
    assert env.spoken == ["Як домовлялись, перевіряю пошту.", "Нагадую: подзвонити мамі"]
    assert notes._load_notes() == []


def test_later_is_a_parsed_action(lin):
    from raphael import llm
    assert llm._find_action("[EMO:happy] Добре. [ACTION:later:30|gmail_unread:]")[:2] == ("later", "30|gmail_unread:")
    assert llm._find_action("[ACTION:mail_watch:from:google]")[:2] == ("mail_watch", "from:google")


# ── Чекати лист ──────────────────────────────────────────────────────────────

class FakeGmail:
    def __init__(self, found):
        self.found, self.queries = found, []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return types.SimpleNamespace(execute=lambda: {"messages": [{"id": "m1"}]} if self.found else {})

    def get(self, userId, id, format):
        return types.SimpleNamespace(execute=lambda: {
            "payload": {"headers": [{"name": "From", "value": "Google AI Studio <noreply@google.com>"},
                                    {"name": "Subject", "value": "Your Gemini API access"}]},
            "snippet": "You now have access"})


@pytest.fixture
def mail(env, monkeypatch):
    from raphael import gmail
    box = FakeGmail(found=False)
    monkeypatch.setattr(gmail, "_gmail_accounts", lambda: [("основна", box)])
    monkeypatch.setattr(gmail, "_gmail_extract_body", lambda full: "Your access is approved. Confirm within 7 days.")
    env.patch("llm_chat", lambda *a, **k: fake_reply("Тобі дали доступ до Gemini API, треба підтвердити за тиждень."))
    gmail.box = box
    return gmail


def test_watch_waits_then_tells_what_the_letter_is_about(mail, env):
    from raphael import notes
    assert "чекаю лист «from:google»" in mail.mail_watch("from:google")
    assert mail._watch_check() == 0                                # ще не прийшов
    assert mail.box.queries[0].startswith("(from:google) after:")
    mail.box.found = True
    assert mail._watch_check() == 1
    assert env.spoken[-1] == ("Прийшов лист, якого ти чекав: від Google AI Studio, тема «Your Gemini API access». "
                              "Тобі дали доступ до Gemini API, треба підтвердити за тиждень.")
    assert notes.load_memory()["mail_watch"] == []                 # один раз


def test_watch_gives_up_after_a_week(mail, monkeypatch):
    from raphael import notes
    mail.mail_watch("from:google")
    monkeypatch.setattr(mail.time, "time", lambda: 10 ** 11)
    mail.box.found = True
    assert mail._watch_check() == 0
    assert notes.load_memory()["mail_watch"] == []


def test_watch_stop(mail):
    from raphael import notes
    mail.mail_watch("from:google")
    mail.mail_watch("from:github")
    assert mail.mail_watch_stop("from:google") == "Не чекаю «from:google», решту далі чекаю."
    assert [w["q"] for w in notes.load_memory()["mail_watch"]] == ["from:github"]
    assert mail.mail_watch_stop("") == "Більше не чекаю."
