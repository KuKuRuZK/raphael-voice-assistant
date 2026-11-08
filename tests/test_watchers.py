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
    page = ("Junior Python Developer at Broscorp \u2013 Djinni\nCandidates\nJobs\nSalaries\nLog In\n"
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
    assert tg.plain("**Keymakr** \u2013 Junior\n## Висновок\nвсе **ок**") == "Keymakr \u2013 Junior\nВисновок\nвсе ок"



# ── Вартові вакансій: збирає код, модель лише каже «так» чи «ні» ──────────────

DJINNI = "https://djinni.co/jobs/?primary_keyword=Python"
LINKEDIN = "https://www.linkedin.com/jobs/search/?keywords=Python&location=Vilnius&f_TPR=r604800"
GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Python&location=Vilnius&f_TPR=r604800"


def test_job_links_and_sources():
    assert watch_mod.job_link("https://djinni.co/jobs/802953-junior-python-developer") == \
        "https://djinni.co/jobs/802953-junior-python-developer/"
    assert watch_mod.job_link("https://jobs.dou.ua/companies/uvik-software/vacancies/376414/?from=list_hot") == \
        "https://jobs.dou.ua/companies/uvik-software/vacancies/376414/"
    assert watch_mod.job_link("https://lt.linkedin.com/jobs/view/mid-python-developer-at-xplicity-4486508096"
                              "?position=1&trackingId=x") == "https://www.linkedin.com/jobs/view/4486508096/"
    assert watch_mod.job_link(DJINNI) is None and watch_mod.job_link("https://djinni.co/jobs/keyword-python/") is None
    goal = (f"Python. Сторінки зі списками: {DJINNI} https://jobs.dou.ua/vacancies/?category=Python&exp=0-1 "
            f"{LINKEDIN} https://www.linkedin.com/jobs/view/4486508096/ https://example.com/jobs")
    assert watch_mod.job_sources(goal) == [DJINNI, "https://jobs.dou.ua/vacancies/?category=Python&exp=0-1", GUEST]
    assert watch_mod.job_sources("знижки на ігри https://store.steampowered.com/") == []


@pytest.fixture
def sites(monkeypatch):
    """Сайти вакансій без мережі: lists[список] → посилання, pages[адреса] → текст або (None, причина)."""
    lists, pages, fetched = {}, {}, []

    def page_links(url):
        got = lists[url]
        return ([], got[1]) if isinstance(got, tuple) else (got, None)

    def page_text(url, limit=10000):
        fetched.append(url)
        got = pages[url]
        return ("", got[1]) if isinstance(got, tuple) else (got, None)
    monkeypatch.setattr(tools, "page_links", page_links)
    monkeypatch.setattr(tools, "page_text", page_text)
    return lists, pages, fetched


EVOWILL = ("Junior Python Developer at Evowill\nМи шукаємо\nFull Remote\nCountries where we consider candidates\n"
           "Worldwide\nДосвід не потрібен\nвід $1000\n")
BROSCORP = ("Junior Python Developer at Broscorp\nПро нас\nFull Remote\nCountries where we consider candidates\n"
            "Ukraine\nДосвід від 1 року\n")


def test_job_watch_checks_every_new_link_and_links_come_from_code(sites):
    lists, pages, fetched = sites
    lists[DJINNI] = ["https://djinni.co/jobs/1-junior-python/", "/about/", "https://djinni.co/jobs/2-python-dev/",
                     "https://djinni.co/jobs/1-junior-python/?ref=x"]
    pages["https://djinni.co/jobs/1-junior-python/"] = EVOWILL
    pages["https://djinni.co/jobs/2-python-dev/"] = BROSCORP
    questions = []

    def judge(prompt):
        questions.append(prompt)
        return ("1 | так | Junior Python Developer, Evowill: Full Remote, весь світ, від $1000 \u2014 https://djinni.co/jobs/9/\n"
                "2 | ні | Broscorp: кандидати лише з України")
    watch = {"id": 1, "goal": f"вакансії Python; віддалено. Сторінки зі списками: {DJINNI}", "seen": []}
    out = watch_mod.check_jobs(watch, judge, sleep=lambda s: None)
    assert fetched == ["https://djinni.co/jobs/1-junior-python/", "https://djinni.co/jobs/2-python-dev/"]
    assert "Worldwide" in questions[0] and "Ukraine" in questions[0] and DJINNI not in questions[0]
    assert "Підходять 1 з 2 нових" in out["report"] and "\n  https://djinni.co/jobs/1-junior-python/" in out["report"]
    assert "Відкинула 1" in out["report"] and "лише з України" in out["report"]       # перший запуск: і відкинуте
    assert "\u2014" not in out["report"] and "jobs/9/" not in out["report"] and out["fit"] == 1
    assert out["seen"] == ["https://djinni.co/jobs/1-junior-python/", "https://djinni.co/jobs/2-python-dev/"]
    watch["seen"] = out["seen"]                                  # завтра те саме: мовчить і модель не питає
    again = watch_mod.check_jobs(watch, lambda p: pytest.fail("нових нема, питати нема про що"), sleep=lambda s: None)
    assert again["report"] == "" and "нових 0" in again["summary"]


def test_job_watch_quiet_when_nothing_fits_after_first_run(sites):
    lists, pages, _ = sites
    lists[DJINNI] = ["https://djinni.co/jobs/2-python-dev/"]
    pages["https://djinni.co/jobs/2-python-dev/"] = BROSCORP
    watch = {"id": 1, "goal": f"Python {DJINNI}", "seen": ["https://djinni.co/jobs/1-old/"]}
    out = watch_mod.check_jobs(watch, lambda p: "1 | ні | лише Україна", sleep=lambda s: None)
    assert out["report"] == "" and out["seen"] == ["https://djinni.co/jobs/2-python-dev/"]


def test_rejected_line_gets_job_name_from_page(sites):
    lists, pages, _ = sites
    lists[DJINNI] = ["https://djinni.co/jobs/1-junior-python/", "https://djinni.co/jobs/2-python-dev/"]
    pages["https://djinni.co/jobs/1-junior-python/"] = EVOWILL
    pages["https://djinni.co/jobs/2-python-dev/"] = "Python Developer at Ajax \u2013 Djinni\nОфіс у Києві\nKyiv\nMiddle\n"
    out = watch_mod.check_jobs({"id": 1, "goal": f"Python {DJINNI}", "seen": []},
                               lambda p: "1 | так | Junior Python Developer, Evowill\n2 | ні | офіс не у Вільнюсі",
                               sleep=lambda s: None)
    assert "- Python Developer at Ajax: офіс не у Вільнюсі" in out["report"]
    assert "Списки: djinni 2." in out["report"]


def test_unanswered_job_is_shown_not_lost(sites):
    lists, pages, _ = sites
    lists[DJINNI] = ["https://djinni.co/jobs/1-junior-python/"]
    pages["https://djinni.co/jobs/1-junior-python/"] = EVOWILL
    watch = {"id": 1, "goal": f"Python {DJINNI}", "seen": ["https://djinni.co/jobs/0-old/"]}
    out = watch_mod.check_jobs(watch, lambda p: "Вибач, не можу.", sleep=lambda s: None)
    assert "глянь сам" in out["report"] and "https://djinni.co/jobs/1-junior-python/" in out["report"]


def test_rate_limited_site_waits_once_then_leaves_rest_for_next_time(sites):
    lists, pages, fetched = sites
    lists[GUEST] = [f"https://lt.linkedin.com/jobs/view/job-{n}?position=1" for n in (1000001, 1000002, 1000003)]
    for n in (1000001, 1000002, 1000003):
        pages[f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{n}"] = (None, "Сторінка відповіла 429.")
    pauses = []
    watch = {"id": 1, "goal": f"Python {LINKEDIN}", "seen": ["x"]}
    out = watch_mod.check_jobs(watch, lambda p: pytest.fail("нічого не відкрилось"), sleep=pauses.append)
    assert watch_mod.RETRY_429 in pauses and len(fetched) == 2      # одна спроба й один повтор, далі не стукає
    assert out["seen"] == [] and out["report"] == ""                 # не запамʼятала: завтра спробує знову
    assert watch_mod.PAUSE["linkedin.com"] in pauses


def test_all_lists_broken_is_said_out_loud(sites):
    lists, _, _ = sites
    lists[DJINNI] = (None, "Сторінка відповіла 403.")
    out = watch_mod.check_jobs({"id": 1, "goal": f"Python {DJINNI}", "seen": ["x"]}, lambda p: "", sleep=lambda s: None)
    assert "Жодна сторінка зі списком не відкрилась" in out["report"] and "403" in out["report"]


def test_same_job_from_two_searches_checked_once(sites):
    lists, pages, fetched = sites
    other = GUEST.replace("Vilnius", "Lithuania")
    lists[GUEST] = ["https://lt.linkedin.com/jobs/view/ai-engineer-4485809357?position=1"]
    lists[other] = ["https://www.linkedin.com/jobs/view/4485809357/"]
    pages["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4485809357"] = \
        "AI Engineer\nDATAHEAD\nVilnius, Lithuania\nHybrid\nMid-Senior level\n"
    goal = f"AI {LINKEDIN} {LINKEDIN.replace('Vilnius', 'Lithuania')}"
    out = watch_mod.check_jobs({"id": 2, "goal": goal, "seen": []}, lambda p: "1 | так | AI Engineer, DATAHEAD",
                               sleep=lambda s: None)
    assert len(fetched) == 1 and out["report"].count("4485809357") == 1


@pytest.mark.parametrize("raw,clean", [
    ("(перевір) | Python developer, Partner Dev: віддалено", "Python developer, Partner Dev: віддалено (перевір)"),
    ("Senior Fullstack Developer, SOFTLOFT: Senior, \u2014, \u2014, \u2014; Senior level",
     "Senior Fullstack Developer, SOFTLOFT: Senior; Senior level"),
    ("Growth Hacker \u2014 LLM API, Spendbase: \u2014, Remote, \u2014, \u2014; не розробка",
     "Growth Hacker - LLM API, Spendbase: Remote; не розробка"),
    ("AI Engineer, Twoday - , офіс у Вільнюсі", "AI Engineer, Twoday, офіс у Вільнюсі"),
])
def test_verdict_line_cleanup(raw, clean):
    assert watch_mod._clean(raw) == clean


def test_hub_runs_job_watch_itself_and_remembers_all_checked(hub, sites, monkeypatch):
    h, sent = hub
    lists, pages, _ = sites
    lists[DJINNI] = ["https://djinni.co/jobs/1-junior-python/", "https://djinni.co/jobs/2-python-dev/"]
    pages["https://djinni.co/jobs/1-junior-python/"] = EVOWILL
    pages["https://djinni.co/jobs/2-python-dev/"] = BROSCORP
    monkeypatch.setattr(watch_mod, "PAUSE_DEFAULT", 0)
    monkeypatch.setattr(h, "judge", lambda p: "1 | так | Junior Python Developer, Evowill\n2 | ні | лише Україна")
    monkeypatch.setattr(h.agent, "run", lambda *a: pytest.fail("вакансії збирає код, не агент"))
    w = h.watches.add(f"вакансії Python; віддалено. Сторінки зі списками: {DJINNI}", {"daily": "09:00"})
    task = h.tasks.add("перевірка", "watch", watch_id=w["id"])
    h.run_job_watch(task)
    assert sent[-1].startswith("👀 Вартовий #1: вакансії Python\n") and "djinni.co/jobs/1-junior-python/" in sent[-1]
    assert set(h.watches.get(w["id"])["seen"]) == {"https://djinni.co/jobs/1-junior-python/",
                                                     "https://djinni.co/jobs/2-python-dev/"}
    assert h.tasks.get(task["id"])["status"] == "done"
    assert "підійшло 1" in watch_mod.listing(h.watches.all())


def test_agent_footer_is_not_remembered_as_seen(hub):
    """Перелік відкритого в кінці звіту несе адреси списків; у «бачене» їм не можна."""
    h, sent = hub
    w = h.watches.add("ціни на RTX", {"daily": "09:00"})
    task = h.tasks.add("перевірка", "watch", watch_id=w["id"])
    report = ("RTX 5070 за 549 €: https://shop.lt/rtx\n\nЩо я справді відкривала (1 сторінок, пошуків 0):\n"
              "- читаю: https://shop.lt/videokartos")
    h._task_finished(task, report)
    assert h.watches.get(w["id"])["seen"] == ["https://shop.lt/rtx"]


def test_page_links_absolute_in_order(monkeypatch):
    import httpx

    class R:
        is_redirect, status_code = False, 200
        headers = {"content-type": "text/html; charset=utf-8"}
        text = ('<a href="/jobs/1-a/">A</a><a href="https://jobs.dou.ua/companies/x/vacancies/2/">B</a>'
                '<a href="/jobs/1-a/">A ще раз</a><a>без адреси</a>')
    monkeypatch.setattr(tools, "public_host", lambda host: True)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    assert tools.page_links("https://djinni.co/jobs/?q=python") == (
        ["https://djinni.co/jobs/1-a/", "https://jobs.dou.ua/companies/x/vacancies/2/"], None)
    monkeypatch.setattr(tools, "public_host", lambda host: False)
    assert tools.page_links("https://djinni.co/jobs/")[0] == []
