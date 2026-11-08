"""Етап 3: вартові за розкладом, ранковий звіт, «поки тебе не було», пошта на сервері (лише читання)."""
import json
from datetime import timedelta

import pytest

from raphael import settings as cfg
from raphael import tg
from cloud import brain as brain_mod
from cloud import hub as hub_mod
from cloud import mail as mail_mod
from cloud import tasks as tasks_mod
from cloud import tools
from cloud import watchers as watch_mod


# ── Розклад людськими словами ────────────────────────────────────────────────

@pytest.mark.parametrize("text,schedule,rest", [
    ("щодня 09:00 нові вакансії Python", {"daily": "09:00"}, "нові вакансії Python"),
    ("щодня о 8.30 ціни на RTX", {"daily": "08:30"}, "ціни на RTX"),
    ("щоранку знижки в Steam", {"daily": "09:00"}, "знижки в Steam"),
    ("щовечора новини про Литву", {"daily": "20:00"}, "новини про Литву"),
    ("кожні 6 год курс євро", {"hours": 6}, "курс євро"),
    ("щогодини статус посилки", {"hours": 1}, "статус посилки"),
    ("просто стеж за чимось", None, "просто стеж за чимось"),
])
def test_schedule_words(text, schedule, rest):
    assert watch_mod.parse_schedule(text) == (schedule, rest)


def test_next_run():
    base = tools.now().replace(hour=10, minute=0, second=0, microsecond=0)
    assert watch_mod.next_run({"daily": "09:00"}, base) == base.replace(hour=9) + timedelta(days=1)
    assert watch_mod.next_run({"daily": "21:30"}, base) == base.replace(hour=21, minute=30)
    assert watch_mod.next_run({"hours": 6}, base) == base + timedelta(hours=6)


def test_due_once_and_remembers_links(tmp_path):
    ws = watch_mod.WatchStore(str(tmp_path / "w.json"))
    w = ws.add("вакансії", {"hours": 6})
    ws.update(w["id"], next=tools.now().isoformat())
    assert [x["id"] for x in ws.due()] == [w["id"]]
    assert ws.due() == []                                          # наступний запуск уже перенесено
    assert ws.remember(w["id"], "нова: https://jobs.lt/1 і https://jobs.lt/2.") == ["https://jobs.lt/1", "https://jobs.lt/2"]
    assert ws.remember(w["id"], "знову https://jobs.lt/1") == []
    assert "https://jobs.lt/1" in watch_mod.task_goal(ws.get(w["id"]))       # агент знає, що вже бачено


# ── Хаб ──────────────────────────────────────────────────────────────────────

class FakeBrain:
    def __init__(self, tmp_path):
        self.reminders = tools.Reminders(str(tmp_path / "r.json"))

    def answer(self, text):
        return "відповідь"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    monkeypatch.setattr(tg, "send_message", lambda chat, text, **kw: sent.append(text))
    monkeypatch.setattr(tg, "api", lambda method, **kw: {"message_id": 1})
    store = tasks_mod.TaskStore(str(tmp_path / "t.json"))
    ws = watch_mod.WatchStore(str(tmp_path / "w.json"))
    h = hub_mod.Hub(hub_mod.Router(), FakeBrain(tmp_path), store=store, watches=ws)
    return h, sent


def msg(text):
    return {"message_id": 1, "from": {"id": 111}, "chat": {"id": 111}, "text": text}


def test_watch_command_starts_first_check_now(hub):
    h, sent = hub
    h.on_message(msg("/watch щоранку нові вакансії Python у Вільнюсі"))
    assert "Вартовий #1" in sent[-1]
    [w] = h.watches.due()
    assert w["goal"] == "нові вакансії Python у Вільнюсі"


def test_watcher_is_quiet_when_nothing_new(hub):
    h, sent = hub
    w = h.watches.add("вакансії", {"daily": "09:00"})
    task = h.tasks.add("перевірка", "watch", watch_id=w["id"])
    before = len(sent)
    assert h._task_finished(task, "Нового нема.") is True and len(sent) == before
    assert h._task_finished(task, "Python Developer у Wix: https://jobs.lt/9") is True
    assert "Вартовий #" in sent[-1] and "https://jobs.lt/9" in sent[-1]
    assert h._task_finished(task, "Знову Python Developer у Wix: https://jobs.lt/9") is True
    assert sent[-1].count("jobs.lt/9") == 1 and len(sent) == before + 1       # те саме вдруге мовчки


def test_unwatch_and_list(hub):
    h, sent = hub
    w = h.watches.add("курс євро", {"hours": 6})
    h.on_message(msg("/watches"))
    assert "курс євро" in sent[-1]
    h.on_message(msg(f"/unwatch {w['id']}"))
    assert h.watches.all() == []


def test_brief_time_can_change_or_stop(hub):
    h, sent = hub
    assert h.brief_time() == hub_mod.BRIEF_DEFAULT
    h.on_message(msg("/brief 07:45"))
    assert h.brief_time() == "07:45"
    h.on_message(msg("/brief off"))
    assert h.brief_time() == "off"


def test_brief_sent_once_a_day(hub, monkeypatch):
    h, sent = hub
    monkeypatch.setattr(h, "morning_brief", lambda: "Доброго ранку!")
    h.on_message(msg("/brief 00:00"))
    stops = iter([False, False, True])
    monkeypatch.setattr(h.stop, "wait", lambda t: next(stops))
    h.brief_loop()
    assert sent.count("Доброго ранку!") == 1


def test_away_summary_for_home(hub):
    h, _ = hub
    h.note_away("розмова: яка погода")
    h.note_away("розмова: що нового")
    h.note_away("доручення #2 готове, звіт у Telegram")
    text = h.away_text()
    assert text.startswith("Поки тебе не було:") and "2 рази" in text and "доручення #2" in text
    assert h.away_text() == ""                                     # перекажи раз
    h.router.take_home(wait=0)                                     # ПК увімкнений: нічого не пишемо
    h.note_away("розмова: привіт")
    assert h.away_text() == ""


def test_home_asks_what_happened(env, lin, monkeypatch):
    from raphael import tg_lin
    env.patch("CLOUD_SSH", "lin@сервер")
    monkeypatch.setattr(tg_lin, "_hub", lambda method, path, payload=None: {"text": "Поки тебе не було: тиша."})
    tg_lin.tell_what_happened()
    assert env.spoken[-1] == "Поки тебе не було: тиша."
    env.patch("LIN_CHANNEL", "telegram")                           # з телефона: там і так усе видно
    tg_lin.tell_what_happened()
    assert env.spoken.count("Поки тебе не було: тиша.") == 1


# ── Пошта на сервері ─────────────────────────────────────────────────────────

def test_mail_text_marks_letters_as_data():
    mails = [{"id": "1", "box": "основна", "from": "Google", "subject": "Доступ до Gemini", "snippet": "Вітаємо",
              "ts": 1762600000000}]
    out = mail_mod.text(mails)
    assert "не вказівки" in out and "Google | Доступ до Gemini" in out


def test_server_lin_reads_mail_without_keys_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_mod, "accounts", lambda: [])
    replies = ["[ACTION:mail:]", "Пошти тут нема."]
    seen = []
    monkeypatch.setattr(brain_mod, "chat", lambda messages, max_tokens=600: seen.append(messages) or replies.pop(0))
    b = brain_mod.Brain(tools.Reminders(str(tmp_path / "r.json")), str(tmp_path / "h.json"))
    b.answer("що в пошті?")
    assert "ключі не видано" in seen[1][-1]["content"]


def test_new_mail_only_after_first_look(monkeypatch):
    batches = [[{"id": "a"}, {"id": "b"}], [{"id": "a"}, {"id": "b"}, {"id": "c"}]]
    monkeypatch.setattr(mail_mod, "find", lambda q=None, limit=10: batches.pop(0))
    watch = mail_mod.MailWatch()
    assert watch.fresh() == []                                     # перший погляд: старі листи не новина
    assert [m["id"] for m in watch.fresh()] == ["c"]


def test_agent_has_no_mail_tools():
    """Агент читає чужі сторінки: з поштою лист-пастка міг би винести її вміст назовні."""
    from cloud import agent
    names = {t["function"]["name"] for t in agent.TOOLS}
    assert not any("mail" in n or "calendar" in n for n in names)


def test_first_check_lists_everything_and_reads_pages_first(tmp_path):
    """2026-11-08 перший запуск сказав «нового нема», не відкривши жодної сторінки з вакансіями."""
    ws = watch_mod.WatchStore(str(tmp_path / "w.json"))
    w = ws.add("вакансії Python: https://djinni.co/jobs/keyword-python/", {"daily": "09:00"})
    first = watch_mod.task_goal(ws.get(w["id"]))
    assert "перша перевірка" in first and "ВІДКРИЙ УСІ ЇХ read_page першими" in first
    ws.remember(w["id"], "https://djinni.co/jobs/1")
    later = watch_mod.task_goal(ws.get(w["id"]))
    assert watch_mod.NOTHING_NEW in later and "https://djinni.co/jobs/1" in later


def test_job_facts_take_only_the_facts(monkeypatch):
    page = ("Junior Python Developer at Broscorp – Djinni\nCandidates\nJobs\nSalaries\nLog In\n"
            "Ми шукаємо людину в команду\nПовністю віддалена робота\nFull Remote\n"
            "Countries where we consider candidates\nМолдова\nДосвід від 1 року\nМи п'ємо каву щоранку\n")
    monkeypatch.setattr(tools, "read_page", lambda url, limit=20000: page)
    facts = tools.job_facts("https://djinni.co/jobs/852620/")
    assert facts.startswith("Junior Python Developer at Broscorp")
    assert "Full Remote" in facts
    assert "Countries where we consider candidates\nМолдова" in facts and "Досвід від 1 року" in facts
    assert "каву" not in facts and "Log In" not in facts


def test_agent_can_check_a_job():
    from cloud import agent
    assert "check_job" in {t["function"]["name"] for t in agent.TOOLS}


def test_telegram_text_without_markdown():
    assert tg.plain("**Keymakr** – Junior\n## Висновок\nвсе **ок**") == "Keymakr – Junior\nВисновок\nвсе ок"
