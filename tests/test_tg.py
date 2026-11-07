"""Telegram-транспорт Лін (raphael/tg.py). Ці тести і є завдання: зробити їх зеленими.

    python -m pytest tests/test_tg.py -q              # усі
    python -m pytest tests/test_tg.py -q -k api       # лише ЗАВДАННЯ 1

Мережі тести не торкаються: підміняють tg._http_post (або tg.api, tg.get_updates).
"""
import threading

import pytest

from raphael import settings as cfg
from raphael import tg

TOKEN = "123456:TEST"


@pytest.fixture
def http(monkeypatch):
    """Підмінений _http_post: записує запити, відповідає тим, що лежить у replies."""
    calls, replies = [], []

    def fake(url, payload, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        return replies.pop(0) if replies else {"ok": True, "result": True}

    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(tg, "_http_post", fake)
    return calls, replies


def message(user_id, text="привіт", update_id=1):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "from": {"id": user_id}, "chat": {"id": user_id}, "text": text}}


# ── ЗАВДАННЯ 1: api, get_updates ─────────────────────────────────────────────

def test_api_calls_method_url_and_returns_result(http):
    calls, replies = http
    replies.append({"ok": True, "result": {"id": 42, "username": "lin_bot"}})
    assert tg.api("getMe") == {"id": 42, "username": "lin_bot"}
    assert calls[0]["url"] == f"https://api.telegram.org/bot{TOKEN}/getMe"


def test_api_sends_params_without_none(http):
    calls, _ = http
    tg.api("sendMessage", chat_id=5, text="hi", reply_markup=None)
    assert calls[0]["payload"] == {"chat_id": 5, "text": "hi"}


def test_api_error_becomes_telegram_error(http):
    _, replies = http
    replies.append({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
    with pytest.raises(tg.TelegramError) as e:
        tg.api("sendMessage", chat_id=1, text="x")
    assert e.value.code == 400
    assert "chat not found" in e.value.description


def test_get_updates_long_polls_with_offset(http):
    calls, replies = http
    replies.append({"ok": True, "result": [message(111, update_id=10)]})
    assert tg.get_updates(offset=10, timeout=30)[0]["update_id"] == 10
    sent = calls[0]
    assert sent["url"].endswith("/getUpdates")
    assert sent["payload"]["offset"] == 10 and sent["payload"]["timeout"] == 30
    assert sent["timeout"] > 30                  # HTTP чекає довше, ніж Telegram тримає запит


def test_get_updates_first_time_without_offset(http):
    calls, _ = http
    tg.get_updates()
    assert "offset" not in calls[0]["payload"]


# ── ЗАВДАННЯ 2: split_text, send_message ─────────────────────────────────────

def test_short_text_is_one_part():
    assert tg.split_text("Привіт, Владе.") == ["Привіт, Владе."]


def test_empty_text_is_nothing():
    assert tg.split_text("") == []
    assert tg.split_text("   \n\n ") == []


def test_split_between_paragraphs_first():
    first, second = "Перший абзац тут.", "Другий абзац тут."
    assert tg.split_text(f"{first}\n\n{second}", limit=20) == [first, second]


def test_split_between_lines_then_words():
    parts = tg.split_text("один два три\nчотири пʼять шість сім вісім", limit=12)
    assert all(0 < len(p) <= 12 for p in parts)
    assert parts[0] == "один два три"
    assert " ".join(parts).split() == "один два три чотири пʼять шість сім вісім".split()   # жодне слово не загубилось
    assert all(p == p.strip() for p in parts)


def test_word_longer_than_limit_is_cut():
    assert tg.split_text("x" * 50, limit=20) == ["x" * 20, "x" * 20, "x" * 10]


def test_real_limit_is_telegram_limit():
    long = ("Слово " * 2000).strip()                 # ≈12 тисяч символів
    parts = tg.split_text(long)
    assert len(parts) >= 3 and all(len(p) <= 4096 for p in parts)


def test_send_message_sends_parts_in_order(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "api", lambda method, **p: sent.append((method, p["chat_id"], p["text"])))
    tg.send_message(7, "А" * 5000)
    assert [m for m, _, _ in sent] == ["sendMessage", "sendMessage"]
    assert all(chat == 7 for _, chat, _ in sent)
    assert "".join(t for _, _, t in sent) == "А" * 5000


def test_send_nothing_for_empty_text(monkeypatch):
    monkeypatch.setattr(tg, "api", lambda *a, **k: pytest.fail("порожнє повідомлення Telegram не прийме"))
    tg.send_message(7, "")


# ── ЗАВДАННЯ 3: is_allowed, poll ─────────────────────────────────────────────

@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])


def test_only_allowed_people(allowed):
    assert tg.is_allowed(message(111)) is True
    assert tg.is_allowed(message(999)) is False


def test_button_press_from_allowed(allowed):
    assert tg.is_allowed({"update_id": 3, "callback_query": {"from": {"id": 111}, "data": "так"}}) is True


def test_update_without_author_is_not_allowed(allowed):
    assert tg.is_allowed({"update_id": 4, "channel_post": {"chat": {"id": -100}, "text": "реклама"}}) is False


def test_empty_allowlist_means_nobody(monkeypatch):
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [])
    assert tg.is_allowed(message(111)) is False


def fake_updates(monkeypatch, batches, stop):
    """get_updates віддає batches по черзі (виняток у списку кидається), offset записує."""
    offsets = []

    def fake(offset=None, timeout=tg.POLL_TIMEOUT):
        offsets.append(offset)
        if not batches:
            stop.set()
            return []
        batch = batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch
    monkeypatch.setattr(tg, "get_updates", fake)
    return offsets


def test_poll_handles_allowed_and_moves_offset(monkeypatch, allowed):
    stop, handled = threading.Event(), []
    offsets = fake_updates(monkeypatch, [[message(111, "привіт", 5), message(999, "спам", 6)]], stop)
    tg.poll(handled.append, stop)
    assert [m["text"] for m in handled] == ["привіт"]          # чужий не дійшов
    assert offsets[0] is None and offsets[1] == 7              # обидва підтверджені: 6 + 1


def test_poll_survives_crashing_handler(monkeypatch, allowed):
    stop = threading.Event()
    offsets = fake_updates(monkeypatch, [[message(111, "отрута", 8)]], stop)

    def crash(msg):
        raise RuntimeError("впав обробник")
    tg.poll(crash, stop)
    assert offsets[1] == 9                     # повідомлення не повернеться вдруге


def test_poll_backs_off_when_network_is_down(monkeypatch, allowed):
    stop, handled, pauses = threading.Event(), [], []
    monkeypatch.setattr(tg.time, "sleep", pauses.append)
    fake_updates(monkeypatch, [OSError("нема мережі"), OSError("нема мережі"), [message(111, "є мережа", 1)]], stop)
    tg.poll(handled.append, stop)
    assert pauses[:2] == [1, 2]
    assert [m["text"] for m in handled] == ["є мережа"]


def test_poll_stops_when_asked(monkeypatch, allowed):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(tg, "get_updates", lambda *a, **k: pytest.fail("stop уже встановлено"))
    tg.poll(lambda m: None, stop)
