"""
Лін у Telegram: повідомлення з телефона йдуть у той самий мозок, що й голос удома.

- Текст і голосові (Whisper) → assistant.process_command(..., channel="telegram").
  Поки Влад пише з телефона, усе, що Лін сказала б уголос (відповідь, погода, пошта,
  нагадування), іде в чат: runtime.REMOTE_SAY. Перша ж голосова команда вдома
  повертає її голос у кімнату.
- Незворотне (вимкнути ПК, стерти плани) питає кнопками «Так / Ні», а не мікрофоном.
- Слухає один потік (tg.poll), а виконує інший (_worker): поки Лін чекає на кнопку,
  слухач мусить ту кнопку отримати, інакше обидва чекали б одне одного.
- Відповідає лише акаунтам з TELEGRAM_ALLOWED_IDS (tg.is_allowed).
"""
import logging
import queue
import threading
import uuid

from raphael import emotion
from raphael import runtime
from raphael import settings as cfg
from raphael import tg

log = logging.getLogger("Лін")

CONFIRM_WAIT = 120          # скільки секунд кнопки «Так / Ні» чекають на відповідь
TYPING_EVERY = 4            # «друкує...» гасне за 5 с, тож оновлюємо частіше

_inbox = queue.Queue()
_pending = {}               # ключ кнопок → (Event, [відповідь])
_stop = threading.Event()


# ── Що Лін каже в чат ────────────────────────────────────────────────────────

def say(text: str) -> None:
    """Сказати в чат Telegram те, що вдома прозвучало б уголос. Тег настрою [EMO:x]
    читає тіло, а в чаті він зайвий."""
    chat = runtime.TG_CHAT_ID
    _, clean = emotion.take(text)
    if not chat or not clean:
        return
    try:
        tg.send_message(chat, clean)
    except Exception as e:
        log.warning(f"Telegram: не надіслала відповідь: {tg._hide_token(e)}")


def confirm(question: str) -> bool:
    """Кнопки «Так / Ні» під питанням; чекає натискання до CONFIRM_WAIT секунд.
    Без відповіді це «ні»: мовчання на незворотне не згода."""
    chat = runtime.TG_CHAT_ID
    if not chat:
        return False
    key = uuid.uuid4().hex[:10]
    done, answer = threading.Event(), []
    _pending[key] = (done, answer)
    try:
        tg.send_message(chat, question, reply_markup={"inline_keyboard": [[
            {"text": "Так", "callback_data": f"{key}:yes"},
            {"text": "Ні", "callback_data": f"{key}:no"}]]})
        done.wait(CONFIRM_WAIT)
    except Exception as e:
        log.warning(f"Telegram: кнопки не надіслались: {tg._hide_token(e)}")
    finally:
        _pending.pop(key, None)
    return answer[:1] == ["yes"]


def _on_button(callback: dict) -> None:
    """Натиснута кнопка: віддати відповідь тому, хто чекає, і прибрати кнопки з чату."""
    key, _, choice = (callback.get("data") or "").partition(":")
    waiting = _pending.get(key)
    try:
        tg.api("answerCallbackQuery", callback_query_id=callback["id"],
               text=None if waiting else "Це питання вже неактуальне")
        msg = callback.get("message") or {}
        if msg:
            tg.api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                   text=f"{msg.get('text', '')}\n→ {'так' if choice == 'yes' else 'ні'}")
    except Exception as e:
        log.debug(f"Telegram: кнопка: {tg._hide_token(e)}")
    if waiting:
        waiting[1].append(choice)
        waiting[0].set()


# ── Вхідні повідомлення ──────────────────────────────────────────────────────

class _Typing:
    """«Лін друкує...» у чаті, поки вона думає."""

    def __init__(self, chat_id):
        self.chat_id, self.done = chat_id, threading.Event()

    def __enter__(self):
        def _loop():
            while not self.done.is_set():
                try:
                    tg.api("sendChatAction", chat_id=self.chat_id, action="typing")
                except Exception:
                    pass
                self.done.wait(TYPING_EVERY)
        threading.Thread(target=_loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.done.set()


def _voice_text(message: dict) -> str:
    """Голосове або аудіо → текст через Whisper."""
    from raphael import stt
    media = message.get("voice") or message.get("audio") or {}
    data = tg.download_file(media["file_id"])
    return stt.transcribe_file(data, "voice.ogg", seconds=media.get("duration", 0))


def handle(message: dict) -> None:
    """Одне повідомлення від Влада: зрозуміти, віддати мозку, відповісти в чат."""
    from raphael import assistant          # assistant тягне мікрофон і pyautogui: не на верху файлу
    chat = message["chat"]["id"]
    runtime.TG_CHAT_ID = chat
    text = (message.get("text") or message.get("caption") or "").strip()
    with _Typing(chat):
        if message.get("voice") or message.get("audio"):
            try:
                text = _voice_text(message)
            except Exception as e:
                log.warning(f"Telegram: голосове не розпізналось: {tg._hide_token(e)}")
                tg.send_message(chat, "Не змогла розібрати голосове, напиши текстом.")
                return
            if not text:
                tg.send_message(chat, "У голосовому нічого не почула.")
                return
            tg.send_message(chat, f"🎤 {text}")          # видно, що саме вона почула
        if not text:
            tg.send_message(chat, "Поки розумію текст і голосові.")
            return
        if text.startswith("/start"):
            runtime.use_channel("telegram")
            tg.send_message(chat, "Я тут. Пиши або надсилай голосові, відповідатиму сюди, поки ти не "
                                  "скажеш щось мені вдома вголос.")
            return
        log.info(f"Telegram: {'голосове' if message.get('voice') else 'повідомлення'}")
        assistant.process_command(assistant._without_name(text), channel="telegram")


def _worker() -> None:
    """Виконує повідомлення по одному, у тому порядку, як прийшли."""
    while not _stop.is_set():
        try:
            message = _inbox.get(timeout=1)
        except queue.Empty:
            continue
        try:
            handle(message)
        except Exception as e:
            log.error(f"Telegram: повідомлення не оброблено: {tg._hide_token(e)}", exc_info=True)
            try:
                tg.send_message(message["chat"]["id"], "Щось пішло не так, деталі в лозі.")
            except Exception:
                pass


def run() -> None:
    """Запустити бота (з lin.py, фоновий потік). Без токена чи списку дозволених мовчки
    нічого не робить: бот без списку відповідав би кожному, хто його знайде."""
    if not cfg.TELEGRAM_BOT_TOKEN:
        return
    if not cfg.TELEGRAM_ALLOWED_IDS:
        log.warning("Telegram: є токен, але нема TELEGRAM_ALLOWED_IDS, бота не запускаю")
        return
    while not _stop.is_set():
        try:
            me = tg.api("getMe")
            break
        except Exception as e:
            log.warning(f"Telegram: бот не відповідає ({tg._hide_token(e)}), ще раз за хвилину")
            _stop.wait(60)
    else:
        return
    runtime.REMOTE_SAY, runtime.REMOTE_CONFIRM = say, confirm
    threading.Thread(target=_worker, daemon=True, name="tg-worker").start()
    log.info(f"Telegram: бот @{me.get('username')} слухає")
    tg.poll(_inbox.put, _stop, on_button=_on_button)


def stop() -> None:
    _stop.set()
    runtime.REMOTE_SAY = runtime.REMOTE_CONFIRM = None
    runtime.use_channel("voice")
