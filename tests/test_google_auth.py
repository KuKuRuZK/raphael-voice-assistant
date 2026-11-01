"""
Відвалений доступ Google: Рафаель каже справжню причину, монітор нагадує
раз на день, а після gmail_auth.bat новий токен підхоплюється без перезапуску.
Бібліотеки Google тут фейкові: токен-файл містить лише {"valid": true/false}.
"""
import json
import sys
import types

import pytest


class RefreshError(Exception):
    pass


class FakeCreds:
    def __init__(self, data):
        self.data = data
        self.refresh_token = "r"

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    @property
    def valid(self):
        return self.data["valid"]

    @property
    def expired(self):
        return not self.data["valid"]

    def refresh(self, request):
        raise RefreshError("('invalid_grant: Token has been expired or revoked.')")

    def to_json(self):
        return json.dumps(self.data)


def _execute(result):
    return types.SimpleNamespace(execute=lambda: result)


class FakeGmail:
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return _execute({"messages": [], "resultSizeEstimate": 0})


class FakeCalendar:
    def events(self):
        return self

    def list(self, **kw):
        return _execute({"items": []})


@pytest.fixture
def google(env, monkeypatch, tmp_path):
    """Одна скринька з токеном, що вже помер. Повертає функцію «увійти знову»."""
    built = []

    def build(name, version, credentials=None):
        built.append((name, credentials))
        return FakeGmail() if name == "gmail" else FakeCalendar()

    for name, attrs in {
        "google": {}, "google.oauth2": {}, "google.auth": {}, "google.auth.transport": {},
        "google.oauth2.credentials": {"Credentials": FakeCreds},
        "google.auth.transport.requests": {"Request": lambda: None},
        "googleapiclient": {}, "googleapiclient.discovery": {"build": build},
    }.items():
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs))

    token = tmp_path / "gmail_token.json"
    token.write_text(json.dumps({"valid": False}))
    creds_file = tmp_path / "gmail_credentials.json"
    creds_file.write_text("{}")
    env.patch("GMAIL_CREDENTIALS_PATH", str(creds_file))
    env.patch("GMAIL_TOKEN_PATH", str(token))
    env.patch("GMAIL_ACCOUNTS", [{"label": "основна", "box": "основна", "token": str(token)}])
    env.patch("_gmail_multi_cache", {})
    env.patch("_google_creds_cache", None)
    env.patch("_calendar_service_cache", None)
    env.patch("_calendar_creds", None)
    env.patch("_auth_dead", {})
    env.patch("_auth_warned_on", "")

    def login_again():
        token.write_text(json.dumps({"valid": True}))
    login_again.built = built
    return login_again


def test_dead_token_is_named_instead_of_not_configured(env, google):
    text = env.get("_gmail_unread_text")()
    assert "Немає доступу до пошти «основна» і календаря" in text
    assert "gmail_auth.bat" in text and "не налаштований" not in text
    assert env.get("_calendar_agenda_text")("today") == env.get("auth_warning")()


def test_new_token_is_picked_up_without_restart(env, google):
    env.get("_gmail_unread_text")()
    env.get("_calendar_agenda_text")("today")
    google()                                                  # gmail_auth.bat
    assert env.get("_gmail_unread_text")() == "Непрочитаних листів немає. Поштова скринька чиста."
    assert env.get("_calendar_agenda_text")("today") == "Сьогодні подій немає."
    assert env.get("_auth_dead") == {}
    assert [name for name, _ in google.built] == ["gmail", "calendar"]


def test_monitor_warns_once_a_day(env, google):
    env.get("_gmail_accounts")()
    warning = env.get("auth_warning")
    assert warning(daily=True)
    assert warning(daily=True) == ""                          # вдруге того ж дня мовчить
    assert warning()                                          # а на пряме питання каже


def test_token_dying_mid_session(env, google):
    def dead(**kw):
        raise RefreshError("invalid_grant")
    svc = FakeGmail()
    svc.list = dead
    env.get("_gmail_multi_cache")[env.get("GMAIL_TOKEN_PATH")] = svc
    text = env.get("_gmail_unread_text")()
    assert "Немає доступу" in text and "чиста" not in text
    assert env.get("_gmail_multi_cache") == {}               # наступна спроба прочитає файл


def test_network_error_is_not_a_clean_inbox(env, google):
    def offline(**kw):
        raise ConnectionError("no route to host")
    svc = FakeGmail()
    svc.list = offline
    env.patch("_gmail_accounts", lambda: [("основна", svc)])
    assert env.get("_gmail_unread_text")() == "Не вдалося перевірити пошту."
