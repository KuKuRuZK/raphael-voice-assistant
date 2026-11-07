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

from cloud import brain as brain_mod
from cloud import tools

log = logging.getLogger("Лін")

PORT = int(os.environ.get("LIN_HUB_PORT", 8790))
HOME_SEEN = 5               # домашня «вдома», якщо питала щонайбільше стільки секунд тому
HOME_GRACE = 15             # стільки секунд повідомлення чекає на домашню, далі відповідає сервер
LONG_POLL = 25              # скільки тримати запит домашньої, коли нічого нового


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

    def take_home(self, wait: float = LONG_POLL) -> list:
        """Запит домашньої: усе, що для неї є, або [] за wait секунд."""
        with self.cond:
            self.waiting += 1
            self.home_seen = self.clock()
            try:
                self.cond.wait_for(lambda: self.home_queue, timeout=wait)
                taken = [u for _, u in self.home_queue]
                self.home_queue.clear()
                return taken
            finally:
                self.waiting -= 1
                self.home_seen = self.clock()

    def reclaim(self, grace: float = HOME_GRACE) -> list:
        """Що домашня так і не забрала: повертаємо серверу."""
        with self.cond:
            old = [u for at, u in self.home_queue if self.clock() - at >= grace]
            self.home_queue = [(at, u) for at, u in self.home_queue if self.clock() - at < grace]
            return old


class Hub:
    def __init__(self, router: Router = None, brain=None):
        self.router = router or Router()
        self.brain = brain or brain_mod.Brain()
        self.server_queue = queue.Queue()
        self.stop = threading.Event()
        self.chat_id = cfg.TELEGRAM_ALLOWED_IDS[0] if cfg.TELEGRAM_ALLOWED_IDS else None
        self.told_offline = False   # «комп вимкнений» кажемо раз на кожну відсутність

    # ── Хто відповідає ──
    def on_message(self, message: dict) -> None:
        self.chat_id = message["chat"]["id"]
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

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/health":
                    return self._send(200, {"ok": True, "home": hub.router.home_online()})
                if url.path != "/home/updates":
                    return self._send(404, {"error": "нема"})
                if self.headers.get("X-Lin-Key") != key:
                    return self._send(403, {"error": "чужий"})
                wait = float(urllib.parse.parse_qs(url.query).get("wait", [LONG_POLL])[0])
                self._send(200, {"updates": hub.router.take_home(min(wait, 50))})

        return ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def run(self) -> None:
        if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_ALLOWED_IDS:
            raise SystemExit("Нема TELEGRAM_BOT_TOKEN чи TELEGRAM_ALLOWED_IDS у secrets.json")
        me = tg.api("getMe")
        server = self.http_server()
        for target in (server.serve_forever, self.server_loop, self.reminder_loop, self.reclaim_loop):
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
