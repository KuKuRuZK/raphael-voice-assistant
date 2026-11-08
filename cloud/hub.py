"""
Хаб Лін на сервері: єдиний, хто слухає Telegram (токен слухає лише одна програма).

Кожне повідомлення від Влада:
- ПК увімкнений (домашня Лін тримає запит /home/updates через SSH-тунель) → у чергу для
  неї: вона відповідає сама, з усім, що вміє вдома (ПК, Spotify, пошта, екран);
- ПК вимкнений → відповідає серверна Лін (cloud.brain).
Надсилати в Telegram можуть обидві: ексклюзивне лише читання (getUpdates).

Повідомлення, яке домашня не забрала за HOME_GRACE секунд (ПК саме вимикався), хаб
забирає назад і відповідає сам, а не губить.

HTTP лише на 127.0.0.1: ззовні до нього не дістатись, лише SSH-тунелем ключем, якому
на сервері дозволено тільки цей порт. Ключ запиту: sha256 від токена бота, він і так
є з обох боків.
"""
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from raphael import settings as cfg
from raphael import tg

from cloud import agent as agent_mod
from cloud import mail as mail_mod
from cloud import brain as brain_mod
from cloud import pcjobs
from cloud import tasks as tasks_mod
from cloud import tools
from cloud import watchers as watch_mod

log = logging.getLogger("Лін")

PORT = int(os.environ.get("LIN_HUB_PORT", 8790))
HOME_SEEN = 5               # домашня «вдома», якщо питала щонайбільше стільки секунд тому
HOME_GRACE = 15             # стільки секунд повідомлення чекає на домашню, далі відповідає сервер
LONG_POLL = 25              # скільки тримати запит домашньої, коли нічого нового

# Команди бота. Telegram у меню показує лише латиницю, тож українські слова теж розуміємо,
# але в меню латинські
COMMANDS = {
    "tasks": "tasks", "справи": "tasks", "доручення": "tasks",
    "task": "task", "доручи": "task", "доручаю": "task", "справа": "task",
    "show": "show", "покажи": "show",
    "cancel": "cancel", "скасуй": "cancel",
    "retry": "retry", "повтори": "retry",
    "watch": "watch", "стеж": "watch", "вартовий": "watch",
    "watches": "watches", "вартові": "watches",
    "unwatch": "unwatch", "нестеж": "unwatch",
    "brief": "brief", "ранок": "brief",
    "mail": "mail", "пошта": "mail", "розбери": "mail",
}
MENU = [("task", "доручити справу: /task що зробити"), ("tasks", "мої доручення"),
        ("show", "кроки справи: /show номер"), ("cancel", "скасувати: /cancel номер"),
        ("retry", "продовжити справу, що стала: /retry номер"),
        ("watch", "вартовий: /watch щодня 09:00 що шукати"), ("watches", "мої вартові"),
        ("unwatch", "прибрати вартового: /unwatch номер"), ("brief", "ранковий звіт: /brief 08:30 чи off"),
        ("mail", "розібрати пошту: важливе сюди, усе прочитаним (/mail undo відкат)")]
BRIEF_DEFAULT = "08:30"     # ранковий звіт у Telegram, якщо Влад не змінив (/brief)


def _peer_alive(sock) -> bool:
    """Чи домашня ще тримає зʼєднання: запит уже прочитано, тож «можна читати» тут
    означає лише кінець зʼєднання (тунель закрився, ПК вимкнувся)."""
    import select
    import socket
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return not readable or sock.recv(1, socket.MSG_PEEK) != b""
    except OSError:
        return False


class Router:
    """Хто відповідає на оновлення: домашня Лін чи сервер."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.cond = threading.Condition()
        self.home_queue = []        # (коли покладено, оновлення)
        self.waiting = 0            # скільки запитів домашньої зараз чекає
        self.home_seen = float("-inf")
        self.nudged = False         # є завдання для ПК: відпустити запит домашньої зараз

    def home_online(self) -> bool:
        with self.cond:
            return self.waiting > 0 or self.clock() - self.home_seen < HOME_SEEN

    def give_home(self, update: dict) -> None:
        with self.cond:
            self.home_queue.append((self.clock(), update))
            self.cond.notify_all()

    def nudge(self) -> None:
        """Завдання для ПК зʼявилось: запит домашньої, що чекає, хай повернеться зараз і забере."""
        with self.cond:
            self.nudged = True
            self.cond.notify_all()

    def take_home(self, wait: float = LONG_POLL, alive=lambda: True):
        """Запит домашньої: усе, що для неї є, або [] за wait секунд. None, якщо домашня
        пішла, поки чекала (alive() став False): тоді вона вже не вдома, і нічого не забрано.

        Без alive() хаб 2026-11-07 ще 25 с вважав «вдома» Лін, яку вже вбили, а повідомлення
        віддав би мертвому зʼєднанню (BrokenPipeError у журналі)."""
        with self.cond:
            self.waiting += 1
            self.home_seen = self.clock()
            gone = False
            try:
                left = wait
                while not self.home_queue and not self.nudged and left > 0:
                    if not alive():
                        gone = True
                        return None
                    self.cond.wait(min(1.0, left))
                    left -= 1.0
                if not alive():
                    gone = True
                    return None
                taken = [u for _, u in self.home_queue]
                self.home_queue.clear()
                self.nudged = False
                return taken
            finally:
                self.waiting -= 1
                if not gone:
                    self.home_seen = self.clock()
                else:
                    self.home_seen = float("-inf")

    def give_back(self, updates: list) -> None:
        """Віддати домашній не вдалось (зʼєднання впало): серверу, без очікування."""
        with self.cond:
            stale = self.clock() - HOME_GRACE
            self.home_queue[:0] = [(stale, u) for u in updates]
            self.home_seen = float("-inf")

    def reclaim(self, grace: float = HOME_GRACE) -> list:
        """Що домашня так і не забрала: повертаємо серверу."""
        with self.cond:
            old = [u for at, u in self.home_queue if self.clock() - at >= grace]
            self.home_queue = [(at, u) for at, u in self.home_queue if self.clock() - at < grace]
            return old


class Hub:
    def __init__(self, router: Router = None, brain=None, store=None, agent=None, watches=None, pc=None):
        self.router = router or Router()
        self.brain = brain or brain_mod.Brain(on_task=self.create_task)
        self.server_queue = queue.Queue()
        self.task_queue = queue.Queue()
        self.stop = threading.Event()
        self.chat_id = cfg.TELEGRAM_ALLOWED_IDS[0] if cfg.TELEGRAM_ALLOWED_IDS else None
        self.told_offline = False   # «комп вимкнений» кажемо раз на кожну відсутність
        self.tasks = store or tasks_mod.TaskStore(os.path.join(brain_mod.DATA, "tasks.json"))
        self.pc = pc or pcjobs.PcJobs(os.path.join(brain_mod.DATA, "pc_jobs.json"))
        self.agent = agent or agent_mod.Agent(self.tasks, notify=self.say, ask=self.ask_question,
                                              on_finish=self._task_finished, pc=self.send_to_pc)
        self.watches = watches or watch_mod.WatchStore(os.path.join(brain_mod.DATA, "watches.json"))
        self.prefs_path = os.path.join(brain_mod.DATA, "prefs.json")
        self.away_path = os.path.join(brain_mod.DATA, "away.json")
        if brain is None:
            self.brain.on_watch = self.create_watch
            self.brain.on_mail_sweep = self.mail_sweep

    # ── Налаштування й «поки тебе не було» (файли в data, переживають перезапуск) ──
    def _json(self, path: str, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default

    def _write(self, path: str, obj) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)

    def note_away(self, event: str) -> None:
        """Подія, поки ПК вимкнений: домашня перекаже, коли він увімкнеться."""
        if self.router.home_online():
            return
        events = self._json(self.away_path, [])
        events.append({"at": tools.now().strftime("%H:%M"), "text": event})
        self._write(self.away_path, events[-30:])

    def away_text(self) -> str:
        """Що сталось без ПК, одним абзацом, і журнал очищено."""
        events = self._json(self.away_path, [])
        if not events:
            return ""
        self._write(self.away_path, [])
        talks = [e for e in events if e["text"].startswith("розмова")]
        other = [e["text"] for e in events if not e["text"].startswith("розмова")]
        parts = []
        if talks:
            parts.append(f"ти писав мені в Telegram {len(talks)} раз" + ("" if len(talks) == 1 else "и")
                         + ", я відповідала з сервера")
        parts += other[-5:]
        return "Поки тебе не було: " + "; ".join(parts) + "."

    def say(self, text: str) -> None:
        if self.chat_id:
            tg.send_message(self.chat_id, tg.plain(text))

    # ── Доручення ──
    def create_task(self, goal: str, source: str = "telegram") -> str:
        goal = (goal or "").strip()
        if len(goal) < 3:
            return "А що саме доручаєш? Напиши, наприклад: /task знайди три найдешевші квитки до Варшави на суботу."
        task = self.tasks.add(goal, source)
        self.task_queue.put((task["id"], None))
        log.info(f"Доручення #{task['id']} ({source})")
        text = f"🕐 #{task['id']} взяла: {goal}\nЗвіт пришлю сюди. Список справ: /tasks"
        if source != "telegram":
            self.say(text)                   # з голосу вдома: у Telegram теж видно, що взяла
        return text

    # ── Вартові ──
    def create_watch(self, text: str, source: str = "telegram") -> str:
        schedule, goal = watch_mod.parse_schedule(text)
        if not schedule or len(goal) < 3:
            return ("Скажи розклад і що шукати: /watch щодня 09:00 нові вакансії Python у Вільнюсі "
                    "(або «щоранку», «щовечора», «кожні 6 год»).")
        w = self.watches.add(goal, schedule)
        self.watches.update(w["id"], next=tools.now().isoformat())     # перша перевірка одразу
        text = (f"👀 Вартовий #{w['id']} ({watch_mod.describe(schedule)}): {goal}\n"
                "Першу перевірку роблю зараз, далі пишу лише про нове. Список: /watches")
        if source != "telegram":
            self.say(text)
        return text

    def _task_finished(self, task: dict, report: str) -> bool:
        """Звіт справи: звичайну хай повідомить агент, вартового вирішуємо тут (тиша, якщо нового нема)."""
        if not task.get("watch_id"):
            self.note_away(f"доручення #{task['id']} готове, звіт у Telegram")
            return False
        watch = self.watches.get(task["watch_id"])
        # Перелік відкритого в кінці звіту (agent._opened) несе адреси самих списків: у памʼять
        # «уже бачене» їм не можна, бо наступного разу агент їх не відкрив би (так було 2026-11-08)
        body = report.split(agent_mod.OPENED_HEAD)[0]
        fresh = self.watches.remember(task["watch_id"], body)
        if watch is None or watch_mod.NOTHING_NEW in body.lower() or (not fresh and "http" in body):
            log.info(f"Вартовий #{task['watch_id']}: нового нема")
            return True
        self.say(f"👀 Вартовий #{watch['id']}: {watch_mod.title(watch['goal'])}\n\n{report}")
        self.note_away(f"вартовий «{watch_mod.title(watch['goal'])[:40]}» знайшов нове")
        return True

    def run_job_watch(self, task: dict) -> None:
        """Вартовий вакансій: збирає й перевіряє код (watchers.check_jobs), модель лише каже «так» чи «ні»."""
        watch = self.watches.get(task["watch_id"])
        if watch is None or task["status"] in ("done", "failed", "cancelled"):
            return
        self.tasks.update(task["id"], status="working")
        name = watch_mod.title(watch["goal"])
        try:
            out = watch_mod.check_jobs(watch, self.judge)
        except agent_mod.Unavailable as e:
            log.warning(f"Вартовий #{watch['id']}: моделі не відповіли: {e}")
            self.tasks.update(task["id"], status="failed", result=f"Моделі не відповіли: {e}")
            self.say(f"⚠️ Вартовий #{watch['id']} ({name}): моделі не відповіли, вакансії перевірю наступного разу.")
            return
        except Exception as e:
            log.error(f"Вартовий #{watch['id']} впав: {e}", exc_info=True)
            self.tasks.update(task["id"], status="failed", result=f"Збій: {type(e).__name__}")
            self.say(f"⚠️ Вартовий #{watch['id']} ({name}): щось зламалось, деталі в журналі сервера.")
            return
        self.watches.mark_seen(watch["id"], out["seen"], out["fit"], out["summary"])
        self.tasks.update(task["id"], status="done", steps=out["steps"], result=out["report"] or out["summary"])
        if out["report"]:
            self.say(f"👀 Вартовий #{watch['id']}: {name}\n\n{out['report']}")
            self.note_away(f"вартовий «{name[:40]}» знайшов нове")
        log.info(f"Вартовий #{watch['id']}: {out['summary']}{'' if out['report'] else ', мовчу'}")

    @staticmethod
    def judge(prompt: str) -> str:
        return agent_mod.chat_tools([{"role": "user", "content": prompt}], use_tools=False).get("content") or ""

    # ── Міст до ПК (етап 4) ──
    def send_to_pc(self, task: dict, call_id: str, tool: str, args: dict) -> None:
        """Агентові потрібен домашній ПК: завдання в чергу; ПК вимкнений, то сказати, що справа чекає."""
        job = self.pc.add(task["id"], call_id, tool, args)
        log.info(f"Справа #{task['id']}: завдання для ПК #{job['id']} ({tool})")
        if self.router.home_online():
            self.router.nudge()
        else:
            self.say(f"💻 #{task['id']} чекає на компʼютер, щоб {pcjobs.describe(tool, args)}. "
                     "Зроблю, щойно ти його ввімкнеш. Скасувати: /cancel " + str(task["id"]))

    def pc_done(self, job_id, result: str) -> bool:
        """Результат з дому: продовжити справу, якщо вона саме на нього й чекала."""
        job = self.pc.finish(job_id, result)
        if not job:
            return False
        task = self.tasks.get(job["task_id"])
        if task and task["status"] == "waiting_pc" and task.get("pending_call") == job["call_id"]:
            log.info(f"Справа #{task['id']}: ПК відповів, продовжую")
            self.task_queue.put((task["id"], {"pc": result}))
        return True

    def watch_loop(self) -> None:
        while not self.stop.wait(30):
            try:
                for w in self.watches.due():
                    goal = (f"Вартовий вакансій #{w['id']}: {watch_mod.title(w['goal'])}"
                            if watch_mod.job_sources(w["goal"]) else watch_mod.task_goal(w))
                    task = self.tasks.add(goal, "watch", watch_id=w["id"])
                    self.task_queue.put((task["id"], None))
                    log.info(f"Вартовий #{w['id']}: перевірка (справа #{task['id']})")
            except Exception as e:
                log.error(f"Вартові: {e}", exc_info=True)

    # ── Ранковий звіт ──
    def brief_time(self) -> str:
        return self._json(self.prefs_path, {}).get("brief", BRIEF_DEFAULT)

    def morning_brief(self) -> str:
        parts = []
        for name, get in (("Погода", lambda: tools.weather("")), ("Новини", lambda: tools.news(""))):
            try:
                parts.append(f"{name}:\n{get()}")
            except Exception as e:
                log.warning(f"Ранковий звіт, {name}: {e}")
        active = [t for t in self.tasks.all() if t["status"] in ("new", "working", "waiting") and not t.get("watch_id")]
        if active:
            parts.append("Доручення:\n" + "\n".join(tasks_mod.line(t) for t in active))
        if mail_mod.accounts():
            try:
                parts.append("Пошта:\n" + mail_mod.text(mail_mod.find(limit=6)))
                parts.append("Календар сьогодні:\n" + mail_mod.events("сьогодні"))
            except Exception as e:
                log.warning(f"Ранковий звіт, пошта: {e}")
        today = [r for r in self.brain.reminders._load() if r["at"][:10] == tools.now().strftime("%Y-%m-%d")]
        if today:
            parts.append("Нагадування сьогодні:\n" + "\n".join(f"{r['at'][11:16]} {r['text']}" for r in today))
        try:
            return brain_mod.chat([{"role": "system", "content": brain_mod.persona()}, {"role": "user", "content":
                "Склади Владу короткий ранковий звіт у Telegram: привітайся у своєму стилі, погода одним реченням, "
                "3-5 головних новин (лише з наведених, з джерелом), важливі листи коротко (хто й про що), події "
                "календаря, доручення й нагадування, якщо є. Звичайний текст "
                "без ** і #, до 15 рядків.\n\n" + "\n\n".join(parts)}], max_tokens=700)
        except Exception as e:
            log.warning(f"Ранковий звіт: модель не відповіла: {e}")
            return "Доброго ранку! Моделі зараз не відповідають, тож коротко:\n\n" + "\n\n".join(parts)[:3000]

    def mail_sweep(self, source: str = "telegram") -> str:
        """Розбір пошти у фоні (сотні листів це хвилина-дві), звіт окремим повідомленням."""
        if not mail_mod.accounts():
            return "Пошти на сервері нема."

        def _run():
            try:
                retell = lambda listing: brain_mod.chat([{"role": "system", "content": brain_mod.persona()},
                                                         {"role": "user", "content": mail_mod.RETELL + "\n\n" + listing}],
                                                        max_tokens=700)
                self.say(mail_mod.sweep(retell))
                self.note_away("розібрала пошту")
            except Exception as e:
                log.error(f"Розбір пошти: {e}", exc_info=True)
                self.say("📬 Розбір пошти зламався, деталі в журналі сервера.")
        threading.Thread(target=_run, daemon=True).start()
        return "📬 Розбираю пошту, за хвилину пришлю, що там важливого."

    def mail_loop(self) -> None:
        """Нові важливі листи в Telegram, поки ПК вимкнений (вдома про пошту каже домашня Лін)."""
        watch = mail_mod.MailWatch()
        while not self.stop.wait(300):
            if not mail_mod.accounts():
                continue
            try:
                fresh = watch.fresh()                # і з увімкненим ПК: щоб потім не вивалити старе
                if fresh and not self.router.home_online():
                    for m in fresh[:5]:
                        self.say(f"📧 [{m['box']}] {m['from']}: {m['subject']}")
                    self.note_away(f"прийшло листів: {len(fresh)}")
            except Exception as e:
                log.warning(f"Пошта: {type(e).__name__}: {e}")

    def brief_loop(self) -> None:
        while not self.stop.wait(30):
            try:
                at = self.brief_time()
                prefs = self._json(self.prefs_path, {})
                today = tools.now().strftime("%Y-%m-%d")
                if at == "off" or prefs.get("brief_sent") == today or tools.now().strftime("%H:%M") < at:
                    continue
                prefs["brief_sent"] = today
                self._write(self.prefs_path, prefs)
                self.say(self.morning_brief())
                self.note_away("надіслала ранковий звіт")
            except Exception as e:
                log.error(f"Ранковий звіт: {e}", exc_info=True)

    def ask_question(self, task: dict, question: str):
        """Питання агента Владу: відповідь реплаєм на це повідомлення продовжить справу."""
        sent = tg.api("sendMessage", chat_id=self.chat_id, text=f"❓ #{task['id']}: {question}\n(відповідай реплаєм)",
                      reply_markup={"force_reply": True, "input_field_placeholder": "Відповідь для Лін"})
        return (sent or {}).get("message_id")

    def task_loop(self) -> None:
        for t in self.tasks.unfinished():            # недороблене до перезапуску служби
            self.task_queue.put((t["id"], None))
        while not self.stop.is_set():
            try:
                task_id, answer = self.task_queue.get(timeout=1)
            except queue.Empty:
                continue
            task = self.tasks.get(task_id)
            watch = self.watches.get(task["watch_id"]) if task and task.get("watch_id") else None
            if isinstance(answer, dict):                    # результат з ПК
                self.agent.run(task_id, pc_result=answer.get("pc", ""))
            elif watch and answer is None and watch_mod.job_sources(watch["goal"]):
                self.run_job_watch(task)
            else:
                self.agent.run(task_id, answer)

    def command(self, text: str) -> bool:
        """/task, /tasks, /show, /cancel, /retry (і українські слова). True, якщо це була команда."""
        word, _, rest = text.partition(" ")
        name = COMMANDS.get(word[1:].split("@")[0].lower())
        if not name:
            return False
        rest = rest.strip()
        number = rest.lstrip("#") if rest.lstrip("#").isdigit() else None
        if name == "tasks" or (name == "task" and not rest):
            self.say(tasks_mod.listing(self.tasks.all()))
        elif name == "task":
            self.say(self.create_task(rest))
        elif name == "show":
            self.say(tasks_mod.details(self.tasks.get(number)) if number else "Яку? /show номер")
        elif name == "cancel":
            task = self.tasks.get(number) if number else None
            if task and task["status"] not in ("done", "failed", "cancelled"):
                self.tasks.update(number, status="cancelled")
                self.pc.cancel_task(number)
                self.say(f"✖️ #{number} скасовано.")
            else:
                self.say("Нема такої активної справи. Список: /tasks")
        elif name == "retry":
            task = self.tasks.get(number) if number else None
            if task and task["status"] == "failed":
                self.tasks.update(number, status="new")
                self.task_queue.put((int(number), None))
                self.say(f"⏳ #{number} продовжую.")
            else:
                self.say("Продовжити можна лише справу, що стала. Список: /tasks")
        elif name == "watch":
            self.say(self.create_watch(rest) if rest else watch_mod.listing(self.watches.all()))
        elif name == "watches":
            self.say(watch_mod.listing(self.watches.all()))
        elif name == "unwatch":
            gone = number and self.watches.remove(number)
            self.say(f"Вартового #{number} прибрала." if gone else "Нема такого вартового. Список: /watches")
        elif name == "mail":
            if rest.lower() in ("undo", "відкат", "поверни"):
                try:
                    self.say(mail_mod.undo())
                except Exception as e:
                    log.error(f"Відкат пошти: {e}", exc_info=True)
                    self.say("Відкат не вдався, деталі в журналі сервера.")
            else:
                self.say(self.mail_sweep())
        elif name == "brief":
            if re.fullmatch(r"\d{1,2}[:.]\d{2}", rest):
                hh, mm = re.split(r"[:.]", rest)
                prefs = self._json(self.prefs_path, {})
                prefs["brief"] = f"{int(hh):02d}:{mm}"
                self._write(self.prefs_path, prefs)
                self.say(f"Ранковий звіт тепер о {prefs['brief']}.")
            elif rest.lower() in ("off", "вимк", "ні", "стоп"):
                prefs = self._json(self.prefs_path, {})
                prefs["brief"] = "off"
                self._write(self.prefs_path, prefs)
                self.say("Ранковий звіт вимкнено. Увімкнути: /brief 08:30")
            elif rest.lower() in ("зараз", "now"):
                self.say(self.morning_brief())
            else:
                self.say(f"Ранковий звіт: {self.brief_time()}. Змінити: /brief 08:30, вимкнути: /brief off, "
                         "просто зараз: /brief зараз")
        return True

    def _answer_task(self, message: dict) -> bool:
        """Відповідь реплаєм на питання агента продовжує справу (і голосом теж)."""
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        task = self.tasks.by_question(reply_to) if reply_to else None
        if not task:
            return False
        text = (message.get("text") or "").strip()

        def _go(text=text):
            media = message.get("voice") or message.get("audio")
            if media and not text:
                try:
                    text = brain_mod.transcribe(tg.download_file(media["file_id"]), media.get("duration", 0))
                except Exception as e:
                    log.warning(f"Голосова відповідь: {tg._hide_token(e)}")
            if not text:
                self.say("Не розібрала відповідь, напиши текстом реплаєм на питання.")
                return
            self.task_queue.put((task["id"], text))
            self.say(f"⏳ #{task['id']}: дякую, продовжую.")
        threading.Thread(target=_go, daemon=True).start()
        return True

    # ── Хто відповідає ──
    def on_message(self, message: dict) -> None:
        self.chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        # Доручення живуть на сервері: команди й відповіді на питання агента хаб бере сам,
        # хоч би ПК і був увімкнений
        if self._answer_task(message) or (text.startswith("/") and self.command(text)):
            return
        if self.router.home_online():
            self.told_offline = False
            self.router.give_home({"message": message})
        else:
            self.server_queue.put(message)

    def on_button(self, callback: dict) -> None:
        if self.router.home_online():
            self.router.give_home({"callback_query": callback})
            return
        try:                        # кнопки бувають лише від домашньої, а її нема
            tg.api("answerCallbackQuery", callback_query_id=callback["id"],
                   text="Комп вимкнений, це питання вже не діє")
        except Exception as e:
            log.debug(f"Кнопка: {tg._hide_token(e)}")

    def reclaim_loop(self) -> None:
        while not self.stop.wait(2):
            for update in self.router.reclaim():
                if "message" in update:
                    log.info("Домашня не забрала повідомлення, відповідає сервер")
                    self.server_queue.put(update["message"])

    # ── Серверна Лін ──
    def handle(self, message: dict) -> None:
        chat = message["chat"]["id"]
        text = (message.get("text") or message.get("caption") or "").strip()
        tg.api("sendChatAction", chat_id=chat, action="typing")
        media = message.get("voice") or message.get("audio")
        if media:
            try:
                text = brain_mod.transcribe(tg.download_file(media["file_id"]), media.get("duration", 0))
            except Exception as e:
                log.warning(f"Голосове: {tg._hide_token(e)}")
                tg.send_message(chat, "Не змогла розібрати голосове, напиши текстом.")
                return
            tg.send_message(chat, f"🎤 {text}" if text else "У голосовому нічого не почула.")
            if not text:
                return
        if not text:
            tg.send_message(chat, "Поки розумію текст і голосові.")
            return
        if not self.told_offline:
            self.told_offline = True
            tg.send_message(chat, "(Комп зараз вимкнений, відповідаю з сервера: розмова, погода, пошук і "
                                  "нагадування працюють, а ПК, музика й пошта будуть, коли він увімкнеться.)")
        if text.startswith("/start"):
            return
        tg.send_message(chat, self.brain.answer(text))
        self.note_away(f"розмова: {text[:60]}")

    def server_loop(self) -> None:
        while not self.stop.is_set():
            try:
                message = self.server_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handle(message)
            except Exception as e:
                log.error(f"Сервер: повідомлення не оброблено: {tg._hide_token(e)}", exc_info=True)

    def reminder_loop(self) -> None:
        while not self.stop.wait(20):
            try:
                for text in self.brain.reminders.due():
                    if self.chat_id:
                        tg.send_message(self.chat_id, f"⏰ {text}")
                        self.note_away(f"нагадала: {text[:50]}")
            except Exception as e:
                log.warning(f"Нагадування: {tg._hide_token(e)}")

    # ── Тунель для домашньої ──
    def http_server(self) -> ThreadingHTTPServer:
        hub, key = self, tg.hub_key()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                """Домашня Лін доручає голосом: POST /tasks {"goal"} чи /watches {"text": "щоранку ..."};
                віддає результат завдання для ПК: POST /home/jobs {"id", "result"}."""
                path = urllib.parse.urlparse(self.path).path
                if path not in ("/tasks", "/watches", "/mail/sweep", "/home/jobs"):
                    return self._send(404, {"error": "нема"})
                if self.headers.get("X-Lin-Key") != key:
                    return self._send(403, {"error": "чужий"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "не JSON"})
                if path == "/home/jobs":
                    return self._send(200, {"ok": hub.pc_done(body.get("id"), str(body.get("result", "")))})
                if path == "/mail/sweep":
                    return self._send(200, {"text": hub.mail_sweep("voice")})
                if path == "/watches":
                    return self._send(200, {"text": hub.create_watch(str(body.get("text", "")), "voice")})
                self._send(200, {"text": hub.create_task(str(body.get("goal", "")), body.get("source", "voice"))})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/health":
                    return self._send(200, {"ok": True, "home": hub.router.home_online()})
                if url.path not in ("/home/updates", "/tasks", "/away"):
                    return self._send(404, {"error": "нема"})
                if self.headers.get("X-Lin-Key") != key:
                    return self._send(403, {"error": "чужий"})
                if url.path == "/tasks":
                    return self._send(200, {"text": tasks_mod.listing(hub.tasks.all())})
                if url.path == "/away":
                    return self._send(200, {"text": hub.away_text()})
                wait = float(urllib.parse.parse_qs(url.query).get("wait", [LONG_POLL])[0])
                if hub.pc.has_pending():            # завдання для ПК чекало, поки вона ввімкнеться
                    wait = 0
                updates = hub.router.take_home(min(wait, 50), alive=lambda: _peer_alive(self.connection))
                if updates is None:
                    return                          # домашня пішла, відповідати нікому
                jobs = hub.pc.for_home()
                try:
                    self._send(200, {"updates": updates, "jobs": jobs})
                except OSError:
                    log.info("Домашня зникла, поки віддавала; повідомлення відповість сервер")
                    hub.router.give_back(updates)
                    hub.pc.unsend([j["id"] for j in jobs])

        return ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def run(self) -> None:
        if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_ALLOWED_IDS:
            raise SystemExit("Нема TELEGRAM_BOT_TOKEN чи TELEGRAM_ALLOWED_IDS у secrets.json")
        me = tg.api("getMe")
        server = self.http_server()
        try:                                # меню команд у Telegram
            tg.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU])
        except Exception as e:
            log.warning(f"Меню команд: {tg._hide_token(e)}")
        for target in (server.serve_forever, self.server_loop, self.reminder_loop, self.reclaim_loop,
                       self.task_loop, self.watch_loop, self.brief_loop, self.mail_loop):
            threading.Thread(target=target, daemon=True).start()
        log.info(f"Хаб: бот @{me.get('username')}, тунель для домашньої на 127.0.0.1:{PORT}")
        tg.poll(self.on_message, self.stop, on_button=self.on_button)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx пише URL з токеном, а пошук (ddgs, primp) твої запити: у журнал сервера не треба
    for noisy in ("httpx", "ddgs", "primp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.makedirs(brain_mod.DATA, exist_ok=True)
    tools.now()                                                # часова зона є, інакше падаємо зараз
    Hub().run()


if __name__ == "__main__":
    main()
