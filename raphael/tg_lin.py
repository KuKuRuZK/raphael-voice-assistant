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
import os
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
        tg.send_message(chat, tg.plain(clean))
    except Exception as e:
        log.warning(f"Telegram: не надіслала відповідь: {tg._hide_token(e)}")


def send_to_vlad(text: str) -> str:
    """Дія tg_send: написати Владу в Telegram з голосу вдома («скинь мені в телеграм...»).
    Приватний чат з людиною має той самий id, що й вона, тож чат відомий і до першого
    повідомлення з телефона. Повертає, що сказати вголос."""
    _, text = emotion.take(text)
    if not text:
        return "А що саме написати?"
    if runtime.REMOTE_SAY is None:
        return "Telegram у мене зараз не підключений."
    chat = runtime.TG_CHAT_ID or (cfg.TELEGRAM_ALLOWED_IDS[0] if cfg.TELEGRAM_ALLOWED_IDS else None)
    try:
        tg.send_message(chat, text)
    except Exception as e:
        log.warning(f"Telegram: tg_send не вдалось: {tg._hide_token(e)}")
        return "Не вийшло надіслати в Telegram."
    return "Скинула тобі в Telegram."


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
    if cfg.CLOUD_SSH:
        log.info(f"Telegram: бот @{me.get('username')}, повідомлення з хаба на сервері")
        _pull_from_hub()
    else:
        log.info(f"Telegram: бот @{me.get('username')} слухає")
        tg.poll(_inbox.put, _stop, on_button=_on_button)


# ── Хаб на сервері ───────────────────────────────────────────────────────────
# Telegram слухає хаб (cloud/hub.py) цілодобово. Поки ПК увімкнений, домашня Лін тримає
# до нього запит через SSH-тунель і забирає свої повідомлення; вимкнувся ПК, і хаб
# відповідає сам. Тунель відкриває домашня (лише вихідне зʼєднання), ключем, якому на
# сервері дозволено тільки порт хаба.

def _ssh_exe() -> str:
    import shutil
    return shutil.which("ssh") or r"C:\Program Files\Git\usr\bin\ssh.exe"


_tunnel = []                # процес ssh, що тримає тунель (один)


def _kill_stale_tunnels() -> int:
    """Прибрати тунелі, що лишились від попередньої Лін. ssh живе окремим процесом: Лін
    вбили чи вона впала, а він висить і тримає порт, і новий тунель уже не відкрити."""
    import psutil
    key, stale = os.path.normcase(cfg.CLOUD_SSH_KEY), 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            args = proc.info["cmdline"] or []
            if (proc.info["name"] or "").lower().startswith("ssh") and "-L" in args \
                    and any(os.path.normcase(a) == key for a in args):
                proc.kill()
                stale += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if stale:
        log.info(f"Хаб: прибрала старі тунелі ({stale})")
    return stale


def _close_tunnel() -> None:
    while _tunnel:
        proc = _tunnel.pop()
        if proc.poll() is None:
            proc.terminate()


def _open_tunnel():
    import subprocess
    cmd = [_ssh_exe(), "-N", "-i", cfg.CLOUD_SSH_KEY,
           "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "IdentitiesOnly=yes",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15",
           "-L", f"127.0.0.1:{cfg.CLOUD_LOCAL_PORT}:127.0.0.1:{cfg.CLOUD_HUB_PORT}", cfg.CLOUD_SSH]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _tunnel.append(proc)
    return proc


def _hub(method: str, path: str, payload: dict = None) -> dict:
    """Запит до хаба на сервері через тунель (доручення). Тунель тримає _pull_from_hub."""
    import httpx
    r = httpx.request(method, f"http://127.0.0.1:{cfg.CLOUD_LOCAL_PORT}{path}", json=payload,
                      headers={"X-Lin-Key": tg.hub_key()}, timeout=15)
    r.raise_for_status()
    return r.json()


def delegate(goal: str) -> str:
    """Дія task_add: «Лін, доручаю тобі ...». Справу робить агент на сервері, звіт у Telegram."""
    if not cfg.CLOUD_SSH:
        return "Доручення працюють через сервер, а його в мене не налаштовано."
    try:
        _hub("POST", "/tasks", {"goal": goal, "source": "voice"})
    except Exception as e:
        log.warning(f"Хаб: доручення не передала: {type(e).__name__}: {e}")
        return "Не достукалась до сервера, доручення не передала."
    return "Взяла. Зроблю сама на сервері, звіт пришлю в Telegram."


def watch(text: str) -> str:
    """Дія watch_add: «Лін, стеж щоранку за новими вакансіями». Вартовий живе на сервері."""
    if not cfg.CLOUD_SSH:
        return "Вартові працюють через сервер, а його в мене не налаштовано."
    try:
        return _hub("POST", "/watches", {"text": text}).get("text") or "Поставила вартового."
    except Exception as e:
        log.warning(f"Хаб: вартовий не поставлений: {type(e).__name__}: {e}")
        return "Не достукалась до сервера, вартового не поставила."


def mail_sweep() -> str:
    """Дія mail_sweep: розбір пошти робить сервер (там ключі й черга), звіт у Telegram."""
    if not cfg.CLOUD_SSH:
        return "Розбір пошти працює через сервер, а його в мене не налаштовано."
    try:
        return _hub("POST", "/mail/sweep", {}).get("text") or "Розбираю пошту."
    except Exception as e:
        log.warning(f"Хаб: розбір пошти: {type(e).__name__}: {e}")
        return "Не достукалась до сервера, пошту не розібрала."


AWAY_DELAY = 25             # с після старту: спершу привітання, потім «поки тебе не було»


def tell_what_happened() -> None:
    """Що було, поки ПК був вимкнений: хаб вів журнал (розмови, доручення, вартові, листи).
    Лише вголос удома: з телефона Влад це все й так бачив у чаті."""
    if not cfg.CLOUD_SSH or runtime.LIN_CHANNEL == "telegram":
        return
    try:
        text = _hub("GET", "/away").get("text")
    except Exception as e:
        log.warning(f"Хаб: що було без ПК, не дізналась: {type(e).__name__}: {e}")
        return
    if text:
        from raphael import tts
        log.info(f"Хаб: {text}")
        tts.speak(text)


def task_list() -> str:
    """Дія task_list: що там з дорученнями."""
    if not cfg.CLOUD_SSH:
        return "Доручень у мене нема: сервер не налаштований."
    try:
        return _hub("GET", "/tasks").get("text") or "Доручень нема."
    except Exception as e:
        log.warning(f"Хаб: список справ: {type(e).__name__}: {e}")
        return "Не достукалась до сервера."


def _take_from_hub(wait: int = 25) -> list:
    import httpx
    r = httpx.get(f"http://127.0.0.1:{cfg.CLOUD_LOCAL_PORT}/home/updates", params={"wait": wait},
                  headers={"X-Lin-Key": tg.hub_key()}, timeout=wait + 15)
    r.raise_for_status()
    return r.json().get("updates", [])


def _dispatch(update: dict) -> None:
    """Оновлення з хаба: та сама обробка, що й з Telegram напряму."""
    if not tg.is_allowed(update):                # хаб уже перевірив, але свої двері теж замикаємо
        return
    if "message" in update:
        _inbox.put(update["message"])
    elif "callback_query" in update:
        _on_button(update["callback_query"])


def _pull_from_hub() -> None:
    _kill_stale_tunnels()
    tunnel, pause, failures, told = None, 2, 0, False
    while not _stop.is_set():
        if tunnel is None or tunnel.poll() is not None:
            if tunnel is not None:
                err = (tunnel.stderr.read() or b"").decode("utf-8", "replace").strip()[-200:]
                log.warning(f"Хаб: тунель закрився ({err or 'без пояснень'}), відкриваю знову за {pause} с")
                _tunnel.clear()
                _stop.wait(pause)
                pause = min(pause * 2, 60)
            tunnel = _open_tunnel()
            _stop.wait(2)                           # ssh треба мить, щоб підняти порт
            continue
        try:
            updates = _take_from_hub()
            if failures:
                log.info("Хаб: звʼязок є")
            failures, pause = 0, 2
            if not told:                    # перший звʼязок після старту: розповісти, що було без ПК
                told = True
                threading.Timer(AWAY_DELAY, tell_what_happened).start()
        except Exception as e:
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                log.warning(f"Хаб не відповідає ({failures} раз поспіль): {type(e).__name__}: {e}")
            _stop.wait(min(2 * failures, 30))
            continue
        for update in updates:
            _dispatch(update)
    _close_tunnel()


def stop() -> None:
    _stop.set()
    _close_tunnel()                 # інакше ssh переживе Лін і триматиме порт
    runtime.REMOTE_SAY = runtime.REMOTE_CONFIRM = None
    runtime.use_channel("voice")
