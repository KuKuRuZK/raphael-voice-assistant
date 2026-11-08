"""Знайдене вартовими одним натиском у трекер Job_Search; cvbankas і нові поля від моделі."""
import csv
import json

import pytest

from raphael import settings as cfg
from raphael import tg
from cloud import hub as hub_mod
from cloud import pcjobs
from cloud import tasks as tasks_mod
from cloud import tools
from cloud import watchers as watch_mod

HEADER = "#,Компанія,Посада,Напрям,Локація,Зарплата/міс,Match%,Чому підходить,ATS ключові слова,Статус,Лінк\n"
ROW = ("1,CrewRed,Junior Python Developer,Python,Remote (EU/UA),,82,Прямо junior Python,\"Python, Git\","
       "Подано 2026-06-29,https://djinni.co/jobs/832465-junior-python-developer/\n"
       "2,Rocket Software,Software Engineer Entry-Level,Python/AI,Vilnius,1795€,80,AI,\"Python, AI\",Подано,"
       "https://en.cvbankas.lt/software-engineer-entry-level-nextgen-academy-vilniuje/1-13966436\n")
FIT = {"url": "https://www.linkedin.com/jobs/view/4485809357/", "company": "DATAHEAD", "title": "AI Engineer",
       "area": "AI/LLM", "place": "Вільнюс, гібрид", "salary": "€5000-6500", "match": 78,
       "why": "LLM і Python, гібрид у Вільнюсі; литовська не потрібна"}


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    from raphael import job_tracker
    path = tmp_path / "Job_Search" / "vacancies_master.csv"
    (tmp_path / "Job_Search" / "backups").mkdir(parents=True)
    path.write_bytes(("﻿" + HEADER + ROW).encode("utf-8"))
    monkeypatch.setattr(cfg, "JOB_TRACKER_CSV", str(path))
    return job_tracker, path


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


# ── Трекер на ПК ─────────────────────────────────────────────────────────────

def test_new_vacancy_goes_to_the_end_as_not_applied(tracker):
    job_tracker, path = tracker
    out = job_tracker.add(FIT)
    assert out.startswith("Додала в трекер #3: DATAHEAD, AI Engineer")
    rows = _rows(path)
    assert rows[-1] == ["3", "DATAHEAD", "AI Engineer", "AI/LLM", "Вільнюс, гібрид", "€5000-6500", "78",
                        "LLM і Python, гібрид у Вільнюсі; литовська не потрібна", "", "Не подано", FIT["url"]]
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\xef\xbb\xbf") == 1 and b"\r\n" not in raw
    assert len(list((path.parent / "backups").iterdir())) == 1                 # копія перед першим записом дня


def test_same_vacancy_is_not_added_twice(tracker):
    job_tracker, path = tracker
    same = dict(FIT, url="https://www.cvbankas.lt/software-engineer-entry-level-nextgen-academy-vilniuje/1-13966436?x=1")
    assert "Уже є в трекері: #2 Rocket Software" in job_tracker.add(same)
    assert len(_rows(path)) == 3


def test_page_text_cannot_become_an_excel_formula(tracker):
    job_tracker, path = tracker
    job_tracker.add(dict(FIT, company='=HYPERLINK("http://evil")', title="+cmd|' /C calc'!A0", why="рядок\nдругий"))
    last = _rows(path)[-1]
    assert last[1] == 'HYPERLINK("http://evil")' and last[2].startswith("cmd") and last[7] == "рядок другий"


def test_strange_file_is_left_alone(tracker, tmp_path):
    job_tracker, path = tracker
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert "незнайомий формат" in job_tracker.add(FIT) and path.read_text(encoding="utf-8") == "a,b,c\n1,2,3\n"
    assert "не знайшла" in job_tracker.add(FIT, path=str(tmp_path / "нема.csv"))
    assert "Без посилання" in job_tracker.add(dict(FIT, url="javascript:alert(1)"))


def test_bridge_writes_only_to_the_tracker_from_settings(tracker, tmp_path, monkeypatch):
    from raphael import pc_bridge
    job_tracker, path = tracker
    monkeypatch.setattr(pc_bridge, "_taken", set())
    elsewhere = tmp_path / "інше.csv"
    out = pc_bridge.execute({"id": 1, "tool": "pc_tracker_add", "args": dict(FIT, path=str(elsewhere))})
    assert out.startswith("Додала") and not elsewhere.exists() and len(_rows(path)) == 4
    assert pc_bridge.classify("pc_tracker_add") == "auto"


# ── Вартовий: cvbankas і поля для трекера ────────────────────────────────────

def test_cvbankas_links_and_lists():
    assert watch_mod.job_link("https://www.cvbankas.lt/qa-engineer-vilniuje/1-14142380?ref=x") == \
        "https://en.cvbankas.lt/qa-engineer-vilniuje/1-14142380"
    assert watch_mod.job_link("https://en.cvbankas.lt/?keyw=python") is None
    assert watch_mod.job_sources("QA https://en.cvbankas.lt/?keyw=qa+automation") == ["https://en.cvbankas.lt/?keyw=qa+automation"]


def test_hidden_characters_in_salary_are_dropped():
    page = "Automation Test Engineer Vilnius, UAB Elgama | CVbankas.lt\nVilnius\n3‌6‌0‌0‌-4‌4‌0‌0‌ €/mon.\n"
    assert "3600-4400 €" in tools.facts_of(page)


def test_fit_line_has_fields_for_the_tracker():
    f = watch_mod._fit_fields("AI Engineer | DATAHEAD | AI/LLM | Вільнюс, гібрид | €5000-6500 | 78% | "
                              "LLM і Python; литовська не потрібна", "AI Engineer\nDATAHEAD\n")
    assert (f["title"], f["company"], f["area"], f["place"], f["salary"], f["match"]) == \
        ("AI Engineer", "DATAHEAD", "AI/LLM", "Вільнюс, гібрид", "€5000-6500", 78)
    assert f["line"] == "AI Engineer, DATAHEAD: Вільнюс, гібрид, €5000-6500. LLM і Python; литовська не потрібна"
    loose = watch_mod._fit_fields("Junior Python, Evowill: віддалено", "Junior Python Developer at Evowill – Djinni\n")
    assert loose["line"] == "Junior Python, Evowill: віддалено" and loose["title"] == "Junior Python Developer at Evowill"


# ── Хаб: кнопки під звітом ───────────────────────────────────────────────────

@pytest.fixture
def hub(tmp_path, monkeypatch):
    sent, calls = [], []
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    monkeypatch.setattr(tg, "send_message", lambda chat, text, **kw: sent.append((text, kw)))
    monkeypatch.setattr(tg, "api", lambda method, **kw: calls.append((method, kw)) or {"message_id": 1})
    h = hub_mod.Hub(hub_mod.Router(), object(), store=tasks_mod.TaskStore(str(tmp_path / "t.json")),
                    watches=watch_mod.WatchStore(str(tmp_path / "w.json")), pc=pcjobs.PcJobs(str(tmp_path / "pc.json")))
    return h, sent, calls


def test_report_has_a_tracker_button_per_fit(hub, monkeypatch):
    h, sent, _ = hub
    w = h.watches.add("вакансії AI. Сторінки зі списками: https://djinni.co/jobs/?q=ai", {"daily": "09:00"})
    monkeypatch.setattr(watch_mod, "check_jobs", lambda watch, judge, **kw: {
        "report": "Підходять 1 з 3 нових:\n1. AI Engineer, DATAHEAD", "seen": [FIT["url"]], "fit": 1,
        "fits": [dict(FIT, line="AI Engineer, DATAHEAD")], "summary": "нових 3, підійшло 1", "steps": []})
    h.run_job_watch(h.tasks.add("перевірка", "watch", watch_id=w["id"]))
    text, kw = sent[-1]
    [[button]] = kw["reply_markup"]["inline_keyboard"]
    assert button["text"] == "📌 1. DATAHEAD у трекер" and button["callback_data"] == "tr:1"


def _press(h, oid=1):
    return {"id": "q1", "from": {"id": 111}, "data": f"tr:{oid}",
            "message": {"message_id": 9, "chat": {"id": 111}, "text": "звіт",
                        "reply_markup": {"inline_keyboard": [[{"text": "📌 1. DATAHEAD у трекер",
                                                               "callback_data": f"tr:{oid}"}]]}}}


def test_button_with_pc_off_waits_and_adds_later(hub):
    h, sent, calls = hub
    oid = h.offer_add(FIT, 1)
    h.on_button(_press(h, oid))
    [job] = h.pc.for_home()
    assert job["tool"] == "pc_tracker_add" and job["args"]["company"] == "DATAHEAD" and "path" not in job["args"]
    answers = [kw["text"] for m, kw in calls if m == "answerCallbackQuery"]
    assert "щойно ввімкнеш" in answers[-1]
    edits = [kw for m, kw in calls if m == "editMessageReplyMarkup"]
    assert edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("🕐")
    h.on_button(_press(h, oid))                                         # натиснув ще раз
    assert "Уже в черзі" in [kw["text"] for m, kw in calls if m == "answerCallbackQuery"][-1]
    assert h.pc.for_home() == []
    h.pc_done(job["id"], "Додала в трекер #112: DATAHEAD, AI Engineer. Статус «Не подано», /apply-jobs її побачить.")
    assert sent[-1][0].startswith("📌 Додала в трекер #112")
    h.on_button(_press(h, oid))
    assert "Уже в трекері" in [kw["text"] for m, kw in calls if m == "answerCallbackQuery"][-1]


def test_failed_add_lets_the_button_work_again(hub):
    h, sent, calls = hub
    oid = h.offer_add(FIT, 1)
    h.router.take_home(wait=0)                                          # ПК на звʼязку
    h.on_button(_press(h, oid))
    assert h.router.nudged
    [job] = h.pc.for_home()
    h.pc_done(job["id"], "Трекер зараз відкритий в іншій програмі (Excel?), не змогла дописати.")
    assert "Excel" in sent[-1][0]
    h.on_button(_press(h, oid))
    assert len(h.pc.for_home()) == 1                                    # нове завдання, а не «уже в черзі»


def test_home_buttons_still_go_home(hub):
    h, _, _ = hub
    h.router.take_home(wait=0)
    h.on_button({"id": "q", "from": {"id": 111}, "data": "a1b2c3d4e5:yes"})
    assert h.router.take_home(wait=0) == [{"callback_query": {"id": "q", "from": {"id": 111}, "data": "a1b2c3d4e5:yes"}}]
