"""Розбір пошти на сервері: усі непрочитані → важливе в Telegram → прочитані, з відкатом."""
import json
import os

import pytest

from cloud import brain as brain_mod
from cloud import mail


class FakeGmail:
    """Мінімальна Gmail API: список, метадані пакетом, batchModify."""

    def __init__(self, letters):
        self.letters, self.modified = letters, []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        ids = [{"id": i} for i in self.letters]
        return type("Q", (), {"execute": lambda s: {"messages": ids}})()

    def get(self, userId, id, format, metadataHeaders):
        frm, subj = self.letters[id]
        return {"id": id, "internalDate": "1762600000000", "snippet": subj,
                "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subj}]}}

    def new_batch_http_request(self, callback):
        calls = []

        class Batch:
            def add(self, response):
                calls.append(response)

            def execute(self):
                for r in calls:
                    callback(None, r, None)
        return Batch()

    def batchModify(self, userId, body):
        self.modified.append(body)
        return type("Q", (), {"execute": lambda s: {}})()


@pytest.fixture
def gmail(monkeypatch):
    main = FakeGmail({"a1": ("Google <no-reply@accounts.google.com>", "Security alert: new sign-in"),
                      "a2": ("Wolt <info@wolt.com>", "-50% на піцу сьогодні")})
    official = FakeGmail({"b1": ("Jonas P <jonas.p@example.com>", "Re: ваша кандидатура"),
                          "b2": ("LinkedIn <jobs-noreply@linkedin.com>", "10 нових вакансій для вас")})
    monkeypatch.setattr(mail, "accounts", lambda: [("основна", "t1"), ("друга", "t2")])
    monkeypatch.setattr(mail, "_service", lambda api, version, token: main if token == "t1" else official)
    return main, official


def test_sweep_tells_important_and_marks_all_read(gmail):
    main, official = gmail
    shown = []
    report = mail.sweep(lambda listing: shown.append(listing) or "Безпека: новий вхід. Рута відповіла.")
    assert "Розібрала 4 непрочитаних" in report and "/mail undo" in report and "Рута відповіла" in report
    important = shown[0]
    assert "Security alert" in important and "Re: ваша кандидатура" in important
    assert "піцу" not in important                                   # реклама не важлива
    assert main.modified == [{"ids": ["a1", "a2"], "removeLabelIds": ["UNREAD"]}]
    assert official.modified == [{"ids": ["b1", "b2"], "removeLabelIds": ["UNREAD"]}]
    saved = json.load(open(os.path.join(brain_mod.DATA, mail.UNDO_FILE), encoding="utf-8"))
    assert saved["ids"] == {"основна": ["a1", "a2"], "друга": ["b1", "b2"]}


def test_undo_returns_them_unread(gmail):
    main, official = gmail
    mail.sweep(lambda listing: "ок")
    assert mail.undo() == "Повернула 4 листів у непрочитані."
    assert main.modified[-1] == {"ids": ["a1", "a2"], "addLabelIds": ["UNREAD"]}
    assert "Нема що повертати" in mail.undo()


def test_read_only_keys_said_honestly(gmail, monkeypatch):
    def denied(ids_by_box, unread):
        raise Exception("HttpError 403: Request had insufficient authentication scopes.")
    monkeypatch.setattr(mail, "_set_unread", denied)
    report = mail.sweep(lambda listing: "ок")
    assert "лише на читання" in report
    assert not os.path.exists(os.path.join(brain_mod.DATA, mail.UNDO_FILE))   # нічого не змінено, відкату нема


def test_nothing_unread(monkeypatch):
    monkeypatch.setattr(mail, "unread_all", lambda limit=500: [])
    assert "нема" in mail.sweep(lambda listing: "ок")


def test_home_asks_server_to_sweep(env, lin, monkeypatch):
    from raphael import actions, tg_lin
    env.patch("CLOUD_SSH", "lin@сервер")
    sent = []
    monkeypatch.setattr(tg_lin, "_hub", lambda method, path, payload=None: sent.append(path) or {"text": "Розбираю."})
    actions.execute_action("[ACTION:mail_sweep:]")
    assert sent == ["/mail/sweep"] and env.spoken[-1] == "Розбираю."


def test_keys_for_server_cannot_send_mail():
    from cloud import google_auth
    assert not any("send" in s or "compose" in s or "settings" in s for s in google_auth.SCOPES)
