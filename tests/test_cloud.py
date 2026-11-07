"""Лін у хмарі (cloud/): хаб вирішує, хто відповідає, серверна Лін відповідає, коли ПК вимкнений."""
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import pytest

from raphael import settings as cfg
from raphael import tg
from cloud import brain as brain_mod
from cloud import hub as hub_mod
from cloud import tools


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def msg(text="привіт", user=111, **extra):
    return {"message_id": 1, "from": {"id": user}, "chat": {"id": user}, "text": text, **extra}


@pytest.fixture
def telegram(monkeypatch):
    """Telegram без мережі: надіслане в sent, виклики api в calls."""
    sent, calls = [], []
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    monkeypatch.setattr(tg, "send_message", lambda chat, text, **kw: sent.append((chat, text)))
    monkeypatch.setattr(tg, "api", lambda method, **kw: calls.append((method, kw)) or True)
    return sent, calls


class FakeBrain:
    def __init__(self, tmp_path):
        self.asked = []
        self.reminders = tools.Reminders(str(tmp_path / "r.json"))

    def answer(self, text):
        self.asked.append(text)
        return f"відповідь на «{text}»"


# ── Router ───────────────────────────────────────────────────────────────────

def test_home_offline_until_it_asks():
    clock = Clock()
    r = hub_mod.Router(clock)
    assert r.home_online() is False
    got = []
    t = threading.Thread(target=lambda: got.extend(r.take_home(wait=2)))
    t.start()
    for _ in range(100):
        if r.waiting:
            break
        time.sleep(0.01)
    assert r.home_online() is True                    # запит висить: ПК увімкнений
    r.give_home({"message": msg()})
    t.join(2)
    assert got == [{"message": msg()}]


def test_home_stays_online_a_moment_between_requests():
    clock = Clock()
    r = hub_mod.Router(clock)
    r.take_home(wait=0)
    clock.t += hub_mod.HOME_SEEN - 1
    assert r.home_online() is True
    clock.t += 2
    assert r.home_online() is False


def test_unclaimed_message_goes_back_to_server():
    clock = Clock()
    r = hub_mod.Router(clock)
    r.give_home({"message": msg("стара")})
    clock.t += hub_mod.HOME_GRACE - 1
    r.give_home({"message": msg("свіжа")})
    clock.t += 2
    assert [u["message"]["text"] for u in r.reclaim()] == ["стара"]
    assert [u["message"]["text"] for _, u in r.home_queue] == ["свіжа"]


# ── Hub ──────────────────────────────────────────────────────────────────────

def test_pc_off_server_answers_and_says_so_once(telegram, tmp_path):
    sent, _ = telegram
    h = hub_mod.Hub(hub_mod.Router(Clock()), FakeBrain(tmp_path))
    h.on_message(msg("як справи"))
    h.on_message(msg("а погода"))
    while not h.server_queue.empty():
        h.handle(h.server_queue.get())
    texts = [t for _, t in sent]
    assert sum("Комп зараз вимкнений" in t for t in texts) == 1
    assert "відповідь на «як справи»" in texts and "відповідь на «а погода»" in texts


def test_pc_on_message_goes_home(telegram, tmp_path):
    clock = Clock()
    router = hub_mod.Router(clock)
    router.take_home(wait=0)                           # домашня щойно питала
    h = hub_mod.Hub(router, FakeBrain(tmp_path))
    h.on_message(msg("що на екрані"))
    assert h.server_queue.empty()
    assert router.take_home(wait=0) == [{"message": msg("що на екрані")}]


def test_button_with_pc_off_is_answered_honestly(telegram, tmp_path):
    _, calls = telegram
    h = hub_mod.Hub(hub_mod.Router(Clock()), FakeBrain(tmp_path))
    h.on_button({"id": "q1", "from": {"id": 111}, "data": "abc:yes"})
    assert calls[-1][0] == "answerCallbackQuery" and "не діє" in calls[-1][1]["text"]


def test_server_hears_voice_notes(telegram, tmp_path, monkeypatch):
    sent, _ = telegram
    monkeypatch.setattr(tg, "download_file", lambda file_id: b"OggS")
    monkeypatch.setattr(brain_mod, "transcribe", lambda data, seconds=0: "нагадай о дев'ятій")
    fake = FakeBrain(tmp_path)
    h = hub_mod.Hub(hub_mod.Router(Clock()), fake)
    h.told_offline = True
    h.handle(msg(text=None, voice={"file_id": "f", "duration": 2}))
    assert (111, "🎤 нагадай о дев'ятій") in sent and fake.asked == ["нагадай о дев'ятій"]


def test_home_tunnel_needs_the_key(telegram, tmp_path, monkeypatch):
    monkeypatch.setattr(hub_mod, "PORT", 0)
    h = hub_mod.Hub(hub_mod.Router(), FakeBrain(tmp_path))
    server = h.http_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/home/updates?wait=0"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url, timeout=5)
        assert e.value.code == 403
        h.router.give_home({"message": msg("через тунель")})
        req = urllib.request.Request(url, headers={"X-Lin-Key": tg.hub_key()})
        body = json.load(urllib.request.urlopen(req, timeout=5))
        assert body["updates"][0]["message"]["text"] == "через тунель"
    finally:
        server.shutdown()


# ── Серверна Лін ─────────────────────────────────────────────────────────────

@pytest.fixture
def brain(tmp_path, monkeypatch):
    replies, seen = [], []

    def fake_chat(messages, max_tokens=600):
        seen.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(brain_mod, "chat", fake_chat)
    b = brain_mod.Brain(tools.Reminders(str(tmp_path / "r.json")), str(tmp_path / "h.json"))
    return b, replies, seen


def test_weather_is_looked_up_and_retold(brain, monkeypatch):
    b, replies, seen = brain
    monkeypatch.setattr(tools, "weather", lambda city: f"{city or 'Vilnius'}: +3, завтра до +5")
    replies += ["[EMO:happy] Зараз гляну. [ACTION:weather:]", "[EMO:smug] Плюс три, завтра до п'яти."]
    assert b.answer("яка завтра погода") == "Плюс три, завтра до п'яти."
    assert "Vilnius: +3, завтра до +5" in seen[1][-1]["content"]


def test_reminder_is_saved_without_second_call(brain):
    b, replies, seen = brain
    replies.append("Окей. [ACTION:remind:60|подзвонити мамі]")
    answer = b.answer("нагадай через годину подзвонити мамі")
    assert "подзвонити мамі" in answer and len(seen) == 1
    assert b.reminders.due(tools.now() + timedelta(minutes=61)) == ["подзвонити мамі"]


def test_models_down_is_said_honestly(brain):
    b, replies, _ = brain
    replies.append(brain_mod.Unavailable("429"))
    assert "не відповідають" in b.answer("привіт")


def test_conversation_is_remembered(brain, tmp_path):
    b, replies, seen = brain
    replies += ["Я Лін.", "Ти питав, хто я."]
    b.answer("хто ти")
    b.answer("що я питав")
    assert {"role": "user", "content": "хто ти"} in seen[1]
    again = brain_mod.Brain(b.reminders, str(tmp_path / "h.json"))     # після перезапуску служби
    assert again.history[-1]["content"] == "Ти питав, хто я."


def test_short_prompt_for_the_free_limit():
    """Домашній промт ≈4,5 тис. токенів і впирався в 8 тис. Groq за хвилину; серверний має бути малим."""
    assert len(brain_mod.system_prompt()) < 6000


# ── Нагадування ──────────────────────────────────────────────────────────────

def test_when_to_remind():
    base = tools.now().replace(hour=20, minute=0, second=0, microsecond=0)
    assert tools.parse_when("30", base) == base + timedelta(minutes=30)
    assert tools.parse_when("21:15", base) == base.replace(hour=21, minute=15)
    assert tools.parse_when("09:00", base) == base.replace(hour=9) + timedelta(days=1)    # уже минуло → завтра
    assert tools.parse_when("завтра 9:30", base) == base.replace(hour=9, minute=30) + timedelta(days=1)
    assert tools.parse_when("коли-небудь", base) is None


def test_reminders_fire_once(tmp_path):
    r = tools.Reminders(str(tmp_path / "r.json"))
    assert "Нагадаю" in r.add("30|винести сміття")
    assert r.due(tools.now()) == []
    later = tools.now() + timedelta(minutes=31)
    assert r.due(later) == ["винести сміття"] and r.due(later) == []


# ── Домашня Лін через хаб ────────────────────────────────────────────────────

def test_home_takes_hub_updates_like_telegram(env, lin, monkeypatch):
    from raphael import tg_lin
    env.patch("TELEGRAM_ALLOWED_IDS", [111])
    pressed = []
    monkeypatch.setattr(tg_lin, "_on_button", pressed.append)
    while not tg_lin._inbox.empty():
        tg_lin._inbox.get()
    tg_lin._dispatch({"message": msg("з хаба")})
    tg_lin._dispatch({"message": msg("чуже", user=999)})
    tg_lin._dispatch({"callback_query": {"id": "q", "from": {"id": 111}, "data": "k:yes"}})
    assert tg_lin._inbox.get_nowait()["text"] == "з хаба" and tg_lin._inbox.empty()
    assert [c["id"] for c in pressed] == ["q"]


def test_home_uses_hub_instead_of_telegram_when_configured(env, lin, monkeypatch):
    from raphael import tg_lin
    env.patch("TELEGRAM_BOT_TOKEN", "123:abc")
    env.patch("TELEGRAM_ALLOWED_IDS", [111])
    env.patch("CLOUD_SSH", "lin@сервер")
    used = []
    monkeypatch.setattr(tg_lin.tg, "api", lambda method, **kw: {"username": "lin_bot"})
    monkeypatch.setattr(tg_lin.tg, "poll", lambda *a, **k: pytest.fail("Telegram слухає хаб, не дім: інакше 409"))
    monkeypatch.setattr(tg_lin, "_pull_from_hub", lambda: used.append("hub"))
    monkeypatch.setattr(tg_lin, "_worker", lambda: None)
    tg_lin.run()
    assert used == ["hub"]
