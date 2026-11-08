"""Етап 4: міст до ПК. Агент на сервері просить, домашня Лін робить за своїми правилами."""
import json
import pathlib
import threading
import time
import urllib.request

import pytest

from raphael import settings as cfg
from raphael import tg
from cloud import agent as agent_mod
from cloud import hub as hub_mod
from cloud import pcjobs
from cloud import tasks as tasks_mod
from cloud import tools


def call(name, cid="c1", **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── Черга завдань для ПК ─────────────────────────────────────────────────────

def test_jobs_wait_in_file_and_are_resent_if_unanswered(tmp_path):
    clock = Clock()
    jobs = pcjobs.PcJobs(str(tmp_path / "pc.json"), clock=clock)
    job = jobs.add(7, "c1", "pc_note", {"title": "Вакансії", "text": "..."})
    again = pcjobs.PcJobs(str(tmp_path / "pc.json"), clock=clock)      # перезапуск служби
    assert again.has_pending()
    assert [j["id"] for j in again.for_home()] == [job["id"]] and again.for_home() == []
    assert not again.has_pending()
    clock.t += pcjobs.RESEND                                            # ПК вимкнувся посеред завдання
    assert [j["id"] for j in again.for_home()] == [job["id"]]
    again.unsend([job["id"]])
    assert again.get(job["id"])["status"] == "queued"
    assert again.finish(job["id"], "Записала")["task_id"] == 7
    assert again.finish(job["id"], "Записала") is None                 # повтор нічого не продовжує


def test_cancelled_task_takes_its_pc_jobs_along(tmp_path):
    jobs = pcjobs.PcJobs(str(tmp_path / "pc.json"))
    jobs.add(7, "c1", "pc_look", {"question": "що на екрані"})
    assert jobs.cancel_task(7) == 1 and jobs.for_home() == [] and jobs.waiting() == []


# ── Агент ────────────────────────────────────────────────────────────────────

@pytest.fixture
def agent(tmp_path, monkeypatch):
    """Агент з підробленою моделлю й мостом: script це кроки моделі, sent те, що пішло на ПК."""
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))
    script, seen, sent, told = [], [], [], []
    monkeypatch.setattr(tools, "search", lambda q, limit=6: f"результати «{q}»")

    def chat(messages, use_tools=True):
        seen.append((list(messages), use_tools))
        return script.pop(0)
    a = agent_mod.Agent(store, notify=told.append, ask=lambda t, q: 1, chat=chat,
                        pc=lambda task, cid, tool, args: sent.append((task["id"], cid, tool, args)))
    return a, store, script, seen, sent, told


def test_agent_waits_for_the_pc_and_goes_on_with_its_answer(agent):
    a, store, script, seen, sent, told = agent
    t = store.add("знайди вакансії й запиши в Obsidian")
    script += [call("pc_note", title="Вакансії", text="Evowill: https://djinni.co/jobs/1/")]
    a.run(t["id"])
    task = store.get(t["id"])
    assert task["status"] == "waiting_pc" and sent == [(t["id"], "c1", "pc_note", {
        "title": "Вакансії", "text": "Evowill: https://djinni.co/jobs/1/"})]
    assert "записати в Obsidian «Вакансії»" in tasks_mod.details(task) and "💻" in tasks_mod.line(task)
    names = {tool["function"]["name"] for tool in seen[0][1]}
    assert {"pc_note", "pc_look", "search"} <= names                    # руки на ПК агент бачить
    assert "pc_look" in seen[0][0][0]["content"]                        # і знає про них з промту
    a.run(t["id"])                                                      # без результату з ПК не рушає
    assert len(seen) == 1
    script += [call("finish", "c2", report="Записала у Вхідні.")]
    a.run(t["id"], pc_result="Записала у «Вхідні» сховища: «Вакансії», 1 рядків.")
    assert store.get(t["id"])["status"] == "done" and told and "Записала" in told[-1]
    tool_msg = [m for m in seen[-1][0] if m.get("role") == "tool"]
    assert tool_msg[-1]["tool_call_id"] == "c1" and "Вхідні" in tool_msg[-1]["content"]


def test_other_calls_of_the_same_step_get_an_answer_too(agent):
    """Модель кличе кілька інструментів разом: без відповіді на кожен продовження впало б 400."""
    a, store, script, seen, sent, _ = agent
    t = store.add("глянь на екран і пошукай")
    step = call("pc_look", question="що на екрані")
    step["tool_calls"].append({"id": "c9", "type": "function",
                               "function": {"name": "search", "arguments": "{\"query\": \"x\"}"}})
    script.append(step)
    a.run(t["id"])
    answers = {m["tool_call_id"]: m["content"] for m in store.get(t["id"])["messages"] if m.get("role") == "tool"}
    assert "c9" in answers and "Не виконано" in answers["c9"] and "c1" not in answers


def test_watchers_and_agent_without_bridge_have_no_pc_hands(tmp_path):
    store = tasks_mod.TaskStore(str(tmp_path / "tasks.json"))
    seen = []

    def chat(messages, use_tools=True):
        seen.append(use_tools)
        return call("finish", report="ок")
    agent_mod.Agent(store, notify=lambda t: None, ask=lambda t, q: 1, chat=chat).run(store.add("справа")["id"])
    bridged = agent_mod.Agent(store, notify=lambda t: None, ask=lambda t, q: 1, chat=chat,
                              pc=lambda *a: pytest.fail("вартовий на ПК не ходить"))
    bridged.run(store.add("ціни", "watch", watch_id=1)["id"])
    assert seen == [True, True]                                         # звичайні TOOLS, без pc_*


# ── Хаб ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def hub(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    monkeypatch.setattr(tg, "send_message", lambda chat, text, **kw: sent.append(text))
    monkeypatch.setattr(tg, "api", lambda method, **kw: {"message_id": 1})
    store = tasks_mod.TaskStore(str(tmp_path / "t.json"))
    h = hub_mod.Hub(hub_mod.Router(), object(), store=store, pc=pcjobs.PcJobs(str(tmp_path / "pc.json")))
    return h, sent


def _waiting_task(h, cid="c1"):
    t = h.tasks.add("запиши в Obsidian")
    h.tasks.update(t["id"], status="waiting_pc", pending_call=cid)
    return h.tasks.get(t["id"])


def test_pc_off_task_says_it_waits(hub):
    h, sent = hub
    task = _waiting_task(h)
    h.send_to_pc(task, "c1", "pc_note", {"title": "Ідея"})
    assert "чекає на компʼютер" in sent[-1] and "/cancel" in sent[-1]
    assert h.pc.has_pending() and not h.router.nudged


def test_pc_on_job_wakes_home_at_once(hub):
    h, sent = hub
    h.router.take_home(wait=0)                                          # домашня щойно питала
    h.send_to_pc(_waiting_task(h), "c1", "pc_say", {"text": "привіт"})
    assert h.router.nudged and sent == []


def test_result_from_home_goes_on_only_with_the_right_task(hub):
    h, _ = hub
    task = _waiting_task(h)
    h.send_to_pc(task, "c1", "pc_note", {"title": "Ідея"})
    [job] = h.pc.for_home()
    assert h.pc_done(job["id"], "Записала") is True
    assert h.task_queue.get_nowait() == (task["id"], {"pc": "Записала"})
    assert h.pc_done(job["id"], "Записала") is False and h.task_queue.empty()     # повтор
    other = h.pc.add(task["id"], "c-old", "pc_note", {})                # справа вже на іншому кроці
    assert h.pc_done(other["id"], "x") is True and h.task_queue.empty()


def test_cancel_drops_pc_jobs(hub):
    h, sent = hub
    task = _waiting_task(h)
    h.send_to_pc(task, "c1", "pc_look", {"question": "?"})
    h.command(f"/cancel {task['id']}")
    assert h.tasks.get(task["id"])["status"] == "cancelled" and h.pc.for_home() == []


def test_nudge_lets_waiting_home_go_at_once():
    router = hub_mod.Router()
    got = []
    t = threading.Thread(target=lambda: got.append(router.take_home(wait=10)))
    started = time.monotonic()
    t.start()
    time.sleep(0.2)
    router.nudge()
    t.join(timeout=3)
    assert got == [[]] and time.monotonic() - started < 3 and not router.nudged


def test_tunnel_carries_jobs_home_and_results_back(hub, monkeypatch):
    h, _ = hub
    monkeypatch.setattr(hub_mod, "PORT", 0)
    task = _waiting_task(h)
    h.send_to_pc(task, "c1", "pc_read_page", {"url": "https://www.linkedin.com/jobs/view/1/"})
    server = h.http_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    key = {"X-Lin-Key": tg.hub_key(), "Content-Type": "application/json"}
    try:
        started = time.monotonic()
        req = urllib.request.Request(base + "/home/updates?wait=20", headers=key)
        body = json.load(urllib.request.urlopen(req, timeout=10))
        assert time.monotonic() - started < 5                            # завдання чекало: віддали одразу
        [job] = body["jobs"]
        assert job["tool"] == "pc_read_page" and body["updates"] == []
        data = json.dumps({"id": job["id"], "result": "текст з дому"}).encode()
        req = urllib.request.Request(base + "/home/jobs", data=data, headers=key, method="POST")
        assert json.load(urllib.request.urlopen(req, timeout=5)) == {"ok": True}
        assert h.task_queue.get_nowait() == (task["id"], {"pc": "текст з дому"})
    finally:
        server.shutdown()


# ── Домашній бік: гальма ─────────────────────────────────────────────────────

@pytest.fixture
def bridge(monkeypatch):
    from raphael import pc_bridge
    monkeypatch.setattr(pc_bridge, "_taken", set())
    monkeypatch.setattr(pc_bridge, "_done", {})
    monkeypatch.setattr(pc_bridge, "_stop", threading.Event())
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    return pc_bridge


@pytest.mark.parametrize("tool", ["pc_do", "pc_delete", "pc_run", "shutdown", ""])
def test_anything_not_listed_is_refused(bridge, tool):
    assert bridge.classify(tool) == "deny"
    assert "Відмовлено" in bridge.execute({"id": 1, "tool": tool, "args": {}}, confirm=lambda *a, **k: True)


def test_screen_only_after_yes(bridge, monkeypatch):
    from raphael import pc
    monkeypatch.setattr(pc, "describe_screen", lambda q, which=None: f"бачу термінал ({which})")
    asked = []
    job = {"id": 1, "task_id": 5, "tool": "pc_look", "args": {"question": "що на екрані"}}
    no = bridge.execute(job, confirm=lambda q, chat=None: asked.append((q, chat)) or False)
    assert "не дозволив" in no and "#5" in asked[0][0] and asked[0][1] == 111
    assert bridge.execute(job, confirm=lambda q, chat=None: True) == "бачу термінал (all)"


def test_home_reads_only_public_pages(bridge, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("до домашньої мережі не мало дійти"))
    for url in ("http://192.168.0.1/", "http://127.0.0.1:1234/v1/models", "file:///C:/Users"):
        out = bridge.execute({"id": 1, "tool": "pc_read_page", "args": {"url": url}})
        assert "не читаю" in out or "лише сторінки http" in out


def test_note_goes_to_inbox_without_touching_the_rest(bridge, tmp_path, monkeypatch):
    from raphael import brain
    monkeypatch.setattr(cfg, "BRAIN_VAULT", str(tmp_path))
    monkeypatch.setattr(brain, "BRAIN_INBOX", str(tmp_path / "Вхідні.md"))
    (tmp_path / "Вхідні.md").write_text("старе\n", encoding="utf-8")
    out = bridge.execute({"id": 1, "task_id": 12, "tool": "pc_note",
                          "args": {"title": "Вакансії на ранок", "text": "Evowill\n\nDATAHEAD, Вільнюс"}})
    text = (tmp_path / "Вхідні.md").read_text(encoding="utf-8")
    assert text.startswith("старе\n") and "Лін, справа #12: Вакансії на ранок\n  Evowill\n  DATAHEAD, Вільнюс\n" in text
    assert "\u2014" not in text and "2 рядків" in out


def test_same_job_twice_is_done_once_but_answered_twice(bridge, monkeypatch):
    """Результат не дійшов, хаб надіслав завдання вдруге: не робити знову, а віддати той самий."""
    from raphael import tts
    said, posted = [], []
    monkeypatch.setattr(tts, "speak", said.append)
    job = {"id": 3, "task_id": 1, "tool": "pc_say", "args": {"text": "[EMO:happy] Привіт, я тут"}}
    bridge.submit(job)
    bridge.submit(job)                                                  # уже взяте: пропускаємо
    t = threading.Thread(target=bridge.worker, args=(lambda jid, res: posted.append((jid, res)),), daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while not posted and time.monotonic() < deadline:
        time.sleep(0.02)
    bridge.submit(job)                                                  # уже зроблене: лише відповідь
    while len(posted) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    bridge.stop()
    t.join(timeout=3)
    assert said == ["Привіт, я тут"] and posted == [(3, "Сказала вголос."), (3, "Сказала вголос.")]


def test_home_gets_jobs_from_the_hub_answer(monkeypatch):
    import httpx
    from raphael import tg_lin

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"updates": [{"message": {"text": "привіт"}}], "jobs": [{"id": 1, "tool": "pc_say"}]}
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    assert tg_lin._take_from_hub() == ([{"message": {"text": "привіт"}}], [{"id": 1, "tool": "pc_say"}])


# ── Вартовий через домашній ПК (етап 5) ──────────────────────────────────────

LI = "https://www.linkedin.com/jobs/search/?keywords=Python&location=Vilnius"
LI_GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Python&location=Vilnius"
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"


@pytest.fixture
def server_blocked(monkeypatch):
    """Сервер LinkedIn не пускає (429), а домашня адреса пускає."""
    asked_home = []
    monkeypatch.setattr(tools, "page_links", lambda url: ([], "Сторінка відповіла 429."))
    monkeypatch.setattr(tools, "page_text", lambda url, limit=10000: ("", "Сторінка відповіла 429."))

    def home(kind, url):
        asked_home.append((kind, url))
        if kind == "links":
            return [f"https://lt.linkedin.com/jobs/view/job-{n}?position=1" for n in (1000001, 1000002)], None
        return "AI Engineer\nDATAHEAD\nVilnius, Lithuania\nHybrid\n", None
    return home, asked_home


def test_watcher_reads_through_home_when_server_is_refused(server_blocked):
    from cloud import watchers as watch_mod
    home, asked_home = server_blocked
    pauses = []
    out = watch_mod.check_jobs({"id": 2, "goal": f"AI {LI}", "seen": []},
                               lambda p: "1 | так | AI Engineer, DATAHEAD: гібрид у Вільнюсі\n2 | ні | senior",
                               sleep=pauses.append, home=home)
    assert asked_home == [("links", LI_GUEST), ("text", POSTING.format(1000001)), ("text", POSTING.format(1000002))]
    assert watch_mod.RETRY_429 not in pauses                     # чекати 30 с не треба: є ПК
    assert out["seen"] == ["https://www.linkedin.com/jobs/view/1000001/", "https://www.linkedin.com/jobs/view/1000002/"]
    assert "LinkedIn читала через домашній ПК" in out["report"] and "Підходять 1 з 2" in out["report"]
    assert any("через домашній ПК (LinkedIn): 3 сторінок" in s for s in out["steps"])


def test_both_addresses_tried_and_site_left_only_after_three_misses(monkeypatch):
    """LinkedIn відмовляє то серверу, то дому навмання: одна сторінка, якої не дала жодна адреса,
    ще не привід кидати сайт; три поспіль уже так."""
    from cloud import watchers as watch_mod
    links = [f"https://lt.linkedin.com/jobs/view/job-{n}?position=1" for n in range(1000001, 1000008)]
    monkeypatch.setattr(tools, "page_links", lambda url: (links, None))
    server_ok = {POSTING.format(1000001), POSTING.format(1000003)}
    home_ok = {POSTING.format(1000002)}
    monkeypatch.setattr(tools, "page_text", lambda url, limit=10000: (
        ("AI Engineer\nX\nVilnius\n", None) if url in server_ok else ("", "Сторінка відповіла 429.")))
    tried = []

    def home(kind, url):
        tried.append(url)
        return ("AI Engineer\nY\nVilnius\n", None) if url in home_ok else ("", "Сторінка відповіла 429.")
    out = watch_mod.collect({"id": 2, "goal": f"AI {LI}", "seen": []}, sleep=lambda s: None, home=home)
    got = [l for l, _ in out["jobs"]]
    assert got == [f"https://www.linkedin.com/jobs/view/{n}/" for n in (1000001, 1000002, 1000003)]
    assert out["blocked"] == ["LinkedIn"] and out["later"] == 4 and out["via_home"] == ["LinkedIn"]
    assert POSTING.format(1000003) in tried                     # після дому першим питали дім, потім сервер
    assert POSTING.format(1000007) not in tried                 # після трьох промахів поспіль далі не стукали


def test_watcher_without_pc_falls_back_to_waiting(server_blocked):
    from cloud import watchers as watch_mod
    pauses = []
    out = watch_mod.check_jobs({"id": 2, "goal": f"AI {LI}", "seen": ["x"]}, lambda p: "",
                               sleep=pauses.append, home=lambda kind, url: ([] if kind == "links" else "", "ПК вимкнений"))
    assert watch_mod.RETRY_429 in pauses and out["seen"] == []
    assert "Жодна сторінка зі списком не відкрилась" in out["report"]


def test_hub_fetches_a_page_through_home_and_waits_for_it(hub):
    h, _ = hub
    assert h.fetch_via_home("text", "https://x.lt/") == ("", "ПК вимкнений")
    h.router.take_home(wait=0)                                          # домашня на звʼязку

    def home_side():
        deadline = time.monotonic() + 5
        while not h.pc.has_pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        [job] = h.pc.for_home()
        assert job["tool"] == "pc_page_links"
        h.pc_done(job["id"], json.dumps({"links": ["https://djinni.co/jobs/1-a/"], "error": None}))
    t = threading.Thread(target=home_side)
    t.start()
    assert h.fetch_via_home("links", "https://djinni.co/jobs/") == (["https://djinni.co/jobs/1-a/"], None)
    t.join(timeout=5)
    assert h.task_queue.empty() and h.pc_waiters == {}


def test_hub_gives_up_on_silent_home(hub, monkeypatch):
    h, _ = hub
    monkeypatch.setattr(hub_mod, "HOME_FETCH_WAIT", 0.2)
    h.router.take_home(wait=0)
    assert h.fetch_via_home("text", "https://x.lt/") == ("", "ПК не відповів")
    assert h.pc.for_home() == [] and h.pc_waiters == {}                 # завдання прибране, не висить


def test_home_gives_watcher_pages_whole_and_only_public(bridge, monkeypatch):
    long_text = "Python " * 3000                                        # довше за звичайний ліміт 6000
    monkeypatch.setattr(tools, "page_text", lambda url, limit=10000: (long_text[:limit], None))
    out = json.loads(bridge.execute({"id": 1, "tool": "pc_page_text", "args": {"url": "https://x.lt/"}}))
    assert out == {"text": long_text[:15000], "error": None}
    monkeypatch.undo()
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("до домашньої мережі не мало дійти"))
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_IDS", [111])
    out = json.loads(bridge.execute({"id": 2, "tool": "pc_page_links", "args": {"url": "http://192.168.0.1/"}}))
    assert out["links"] == [] and "не читаю" in out["error"]


def test_second_copy_leaves_watchdog_alone(lin, tmp_path, monkeypatch):
    """2026-11-08 друга копія записала .lin_stop, і живу Лін після збою вже ніхто б не підняв."""
    import ctypes
    marker = tmp_path / ".lin_stop"
    monkeypatch.setattr(lin.cfg, "STOP_MARKER", str(marker))
    monkeypatch.setattr(lin.cfg, "_load_config", lambda: None)
    monkeypatch.setattr(lin.stt, "apply_settings", lambda: None)
    monkeypatch.setattr(lin.tts, "apply_settings", lambda: None)
    monkeypatch.setattr(lin, "_ensure_single_instance", lambda: False)
    shown = []
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *a: shown.append(a[1]))
    with pytest.raises(SystemExit) as e:
        lin.main()
    assert e.value.code == lin.DUPLICATE_EXIT == 3 and not marker.exists() and "вже запущена" in shown[0]
    bat = (pathlib.Path(lin.__file__).parent / "start.bat").read_text()
    assert "if %errorlevel% EQU 3 goto end" in bat
