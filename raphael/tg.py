"""
Telegram для Лін: тонкий клієнт Bot API, без сторонніх бібліотек для ботів.

Як працює бот Telegram:
- Bot API це звичайний HTTPS. POST на https://api.telegram.org/bot<токен>/<метод> з JSON,
  у відповідь {"ok": true, "result": ...} або {"ok": false, "error_code": 400,
  "description": "Bad Request: chat not found"}. Методи: getMe, getUpdates, sendMessage...
  Довідник: https://core.telegram.org/bots/api
- Нові повідомлення забираємо long polling: getUpdates тримає запит відкритим до timeout
  секунд, поки щось не прийде. Кожне оновлення має update_id. Наступний запит з
  offset = останній update_id + 1 каже Telegram «ці я обробив», і він їх більше не віддає.
- Webhook (Telegram сам стукає до нас) не підходить: ні вдома, ні на сервері Oracle
  вхідних портів нема, а long polling ходить лише назовні.
- Один токен слухає лише одна програма. Друга отримає 409 Conflict.
- Бот не може написати першим: людина має натиснути Start у чаті з ним.
- Одне повідомлення не довше за 4096 символів.

Тести: python -m pytest tests/test_tg.py -q
Перший бот (повторює, що йому пишуть): python -m raphael.tg
Лін у Telegram (мозок, голосові, кнопки) живе в raphael/tg_lin.py.
"""
import logging
import threading
import time

from raphael import settings as cfg

log = logging.getLogger("Лін")

API_URL = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
MAX_TEXT = 4096         # найдовше повідомлення, яке приймає Telegram
POLL_TIMEOUT = 30       # скільки секунд getUpdates чекає на нові повідомлення
BACKOFF_MAX = 60        # найдовша пауза між спробами, коли мережа лежить


class TelegramError(Exception):
    """Telegram відповів {"ok": false}. code і description беремо з його відповіді."""

    def __init__(self, code: int, description: str):
        super().__init__(f"Telegram {code}: {description}")
        self.code = code
        self.description = description


def hub_key(token: str = None) -> str:
    """Ключ запитів домашньої Лін до хаба на сервері (cloud/hub.py): похідний від токена
    бота, тож окремого секрету не треба, а без токена хаб нікому нічого не віддасть."""
    import hashlib
    return hashlib.sha256(("lin-hub:" + (token or cfg.TELEGRAM_BOT_TOKEN)).encode()).hexdigest()


def _hide_token(text) -> str:
    """Текст помилки без токена: httpx інколи кладе URL у повідомлення, а з токеном
    будь-хто, хто побачить лог, керуватиме ботом."""
    text = str(text)
    return text.replace(cfg.TELEGRAM_BOT_TOKEN, "<токен>") if cfg.TELEGRAM_BOT_TOKEN else text


# ── Розмова з Bot API ────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, timeout: float) -> dict:
    """Єдине місце, де бот ходить у мережу з запитами (тести підміняють саме її).
    Telegram на помилки відповідає 400/401/409 з тим самим JSON ("ok": false), тож
    тіло повертаємо за будь-якого статусу."""
    import httpx
    return httpx.post(url, json=payload, timeout=timeout).json()


def _http_get(url: str, timeout: float) -> bytes:
    """Завантажити файл (голосове) з серверів Telegram."""
    import httpx
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def api(method: str, http_timeout: float = 10, **params):
    """Викликати метод Bot API і повернути його "result". Параметри зі значенням None
    не надсилаються (Telegram на null у деяких полях відповідає 400), а {"ok": false}
    стає TelegramError. http_timeout названо так, щоб не зіткнутись із параметром
    timeout самого getUpdates."""
    reply = _http_post(API_URL.format(token=cfg.TELEGRAM_BOT_TOKEN, method=method),
                       {k: v for k, v in params.items() if v is not None}, http_timeout)
    if not reply.get("ok"):
        raise TelegramError(reply.get("error_code", 0), reply.get("description", "без опису"))
    return reply.get("result")


def get_updates(offset: int = None, timeout: int = POLL_TIMEOUT) -> list:
    """Нові оновлення (getUpdates з long polling). HTTP чекає на 10 с довше, ніж Telegram
    законно тримає запит, інакше обривалось би кожне очікування."""
    return api("getUpdates", http_timeout=timeout + 10, offset=offset, timeout=timeout,
               allowed_updates=["message", "callback_query"]) or []


def download_file(file_id: str) -> bytes:
    """Вміст файлу з повідомлення (голосове, документ) за його file_id."""
    path = api("getFile", file_id=file_id)["file_path"]
    return _http_get(FILE_URL.format(token=cfg.TELEGRAM_BOT_TOKEN, path=path), timeout=60)


# ── Довгі відповіді ──────────────────────────────────────────────────────────

def split_text(text: str, limit: int = MAX_TEXT) -> list:
    """Розрізати текст на частини не довші за limit: спершу між абзацами, потім між
    рядками, потім між словами; посеред слова лише тоді, коли слово довше за limit.
    Пробіли на межах прибрано, порожніх частин нема, порожній текст → []."""
    text, parts = (text or "").strip(), []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit + 1)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit                     # одне слово довше за limit: ріжемо по живому
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return [p for p in parts if p]


def send_message(chat_id: int, text: str, **extra) -> None:
    """Надіслати текст у чат; довгий частинами split_text, по порядку. extra (кнопки)
    чіпляється до останньої частини."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        api("sendMessage", chat_id=chat_id, text=part, **(extra if i == len(parts) - 1 else {}))


# ── Головний цикл ────────────────────────────────────────────────────────────

def _author(update: dict):
    for key in ("message", "callback_query"):
        who = (update.get(key) or {}).get("from", {}).get("id")
        if who is not None:
            return who
    return None


def is_allowed(update: dict) -> bool:
    """Чи оновлення від акаунта з cfg.TELEGRAM_ALLOWED_IDS (повідомлення або натиснута
    кнопка). Порожній список означає «нікому»: бот без налаштувань світу не відповідає."""
    who = _author(update)
    return who is not None and who in cfg.TELEGRAM_ALLOWED_IDS


def poll(handle, stop: threading.Event, on_button=None) -> None:
    """Слухати Telegram, поки не встановлено stop.

    Дозволене повідомлення → handle(message), натиснута кнопка → on_button(callback_query).
    Чуже пропускаємо (лише в лог). offset просуваємо завжди, навіть якщо обробник впав:
    інакше одне «отруйне» повідомлення поверталося б вічно. Помилка мережі цикл не
    валить: пауза 1, 2, 4... до BACKOFF_MAX секунд, після вдалого запиту знову з 1."""
    offset, pause = None, 1
    while not stop.is_set():
        try:
            updates = get_updates(offset)
            pause = 1
        except Exception as e:
            log.warning(f"Telegram: {_hide_token(e)}; ще раз за {pause} с")
            time.sleep(pause)
            pause = min(pause * 2, BACKOFF_MAX)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            if not is_allowed(update):
                log.warning(f"Telegram: чужий акаунт {_author(update)}, ігнорую")
                continue
            try:
                if "message" in update:
                    handle(update["message"])
                elif "callback_query" in update and on_button:
                    on_button(update["callback_query"])
            except Exception as e:
                log.error(f"Telegram: обробник впав: {_hide_token(e)}", exc_info=True)


# ── Перший бот ───────────────────────────────────────────────────────────────

def _echo(message: dict) -> None:
    """Повторює текст. Перевірка, що транспорт працює, без мозку Лін."""
    send_message(message["chat"]["id"], message.get("text") or "Поки розумію лише текст.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx пише URL з токеном
    if not cfg.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Нема TELEGRAM_BOT_TOKEN: додай у secrets.json (див. secrets.example.json)")
    me = api("getMe")
    print(f"Бот @{me['username']} слухає. Напиши йому в Telegram, Ctrl+C щоб зупинити.")
    try:
        poll(_echo, threading.Event())
    except KeyboardInterrupt:
        pass
