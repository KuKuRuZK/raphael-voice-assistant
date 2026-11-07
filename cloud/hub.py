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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from raphael import settings as cfg
from raphael import tg

from cloud import agent as agent_mod
from cloud import brain as brain_mod
from cloud import tasks as tasks_mod
from cloud import tools

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
}
MENU = [("task", "доручити справу: /task що зробити"), ("tasks", "мої доручення"),
        ("show", "кроки справи: /show номер"), ("cancel", "скасувати: /cancel номер"),
        ("retry", "продовжити справу, що стала: /retry номер")]


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

    def home_online(self) -> bool:
        with self.cond:
            return self.waiting > 0 or self.clock() - self.home_seen < HOME_SEEN

    def give_home(self, update: dict) -> None:
        with self.cond:
            self.home_queue.append((self.clock(), update))
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
                while not self.home_queue and left > 0:
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
    def __init__(self, router: Router = None, brain=None, store=None, agent=None):
        self.router = router or Router()
        self.brain = brain or brain_mod.Brain(on_task=self.create_task)
        self.server_queue = queue.Queue()
        self.task_queue = queue.Queue()
        self.stop = threading.Event()
        self.chat_id = cfg.TELEGRAM_ALLOWED_IDS[0] if cfg.TELEGRAM_ALLOWED_IDS else None
        self.told_offline = False   # «комп вимкнений» кажемо раз на кожну відсутність
        self.tasks = store or tasks_mod.TaskStore(os.path.join(brain_mod.DATA, "tasks.json"))
        self.agent = agent or agent_mod.Agent(self.tasks, notify=self.say, ask=self.ask_question)

    def say(self, text: str) -> None:
        if self.chat_id:
            tg.send_message(self.chat_id, text)

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
                """Домашня Лін доручає справу голосом: POST /tasks {"goal": "..."}."""
                if urllib.parse.urlparse(self.path).path != "/tasks":
                    return self._send(404, {"error": "нема"})
                if self.headers.get("X-Lin-Key") != key:
                    return self._send(403, {"error": "чужий"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "не JSON"})
                self._send(200, {"text": hub.create_task(str(body.get("goal", "")), body.get("source", "voice"))})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/health":
                    return self._send(200, {"ok": True, "home": hub.router.home_online()})
                if url.path not in ("/home/updates", "/tasks"):
                    return self._send(404, {"error": "нема"})
                if self.headers.get("X-Lin-Key") != key:
                    return self._send(403, {"error": "чужий"})
                if url.path == "/tasks":
                    return self._send(200, {"text": tasks_mod.listing(hub.tasks.all())})
                wait = float(urllib.parse.parse_qs(url.query).get("wait", [LONG_POLL])[0])
                updates = hub.router.take_home(min(wait, 50), alive=lambda: _peer_alive(self.connection))
                if updates is None:
                    return                          # домашня пішла, відповідати нікому
                try:
                    self._send(200, {"updates": updates})
                except OSError:
                    log.info("Домашня зникла, поки віддавала; повідомлення відповість сервер")
                    hub.router.give_back(updates)

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
                       self.task_loop):
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
