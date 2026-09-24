import asyncio
import difflib
import hashlib
import itertools
import json
import logging
import logging.handlers
import math
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
from datetime import datetime, timedelta

# ── Падіння мають лишати слід ─────────────────────────────────────────────────
# Під pythonw немає консолі (sys.stderr = None). Помилка до налаштування логів,
# наприклад не встановлена бібліотека, зникала безслідно, а start.bat тихо
# перезапускав Рафаеля кожні 5 секунд. Тепер traceback іде в crash.log.
import sys
import traceback
_PYTHONW = sys.stderr is None or os.path.basename(sys.executable).lower() == "pythonw.exe"
if _PYTHONW:
    try:
        sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log"),
                          "a", encoding="utf-8", buffering=1)
        import faulthandler
        faulthandler.enable(sys.stderr)       # падіння всередині C-бібліотек (pygame, pyaudio)
    except Exception:
        pass


def _crash_hook(tp, val, tb):
    try:
        sys.stderr.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        traceback.print_exception(tp, val, tb)
    except Exception:
        pass


sys.excepthook = _crash_hook

import edge_tts
import psutil
import pyautogui
import pygame
import speech_recognition as sr
from groq import Groq
from PIL import Image, ImageDraw
import pystray

try:
    import keyboard as _keyboard   # глобальні гарячі клавіші (push-to-talk)
except Exception:
    _keyboard = None

# Преміум-орб (окремий модуль): прозоре шарове вікно зі справжнім сяйвом.
# Якщо модуль/платформа недоступні — _PREMIUM_ORB=False і працює запасний tkinter-орб.
try:
    import raphael_orb as _orb
    _PREMIUM_ORB = bool(_orb.AVAILABLE)
except Exception:
    _orb = None
    _PREMIUM_ORB = False

# Сортувальник пошти (окремий модуль, щоб правила можна було правити й тестувати
# не чіпаючи Рафаеля). Немає модуля — монітор просто працює як раніше, без міток.
try:
    import mail_triage
except Exception:
    mail_triage = None

# Розбір фраз: ім'я, «повтори», миттєві команди, час нагадувань, відмінки.
# Чисті функції з тестами (tests/test_voice_rules.py).
import voice_rules as vr
import spotify_common

pyautogui.FAILSAFE = False

# ── Жодних вікон консолі для дочірніх процесів ────────────────────────────────
# PowerShell/shutdown/rundll32 тощо за замовчуванням блимають чорним вікном.
# Патчимо subprocess.Popen, щоб ВСІ дочірні процеси стартували без вікна.
if os.name == "nt":
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _orig_popen_init = subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        try:
            kwargs["creationflags"] = (kwargs.get("creationflags") or 0) | _CREATE_NO_WINDOW
        except Exception:
            pass
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_init_no_window

# ============================================================
#  КОНФІГ
# ============================================================
# ── Секрети (ключі/токени) — НЕ зберігаються в коді ───────────────────────────
# Джерела за пріоритетом: 1) змінна середовища; 2) secrets.json поруч із lin.py.
# Приклад secrets.json: {"GROQ_API_KEY": "gsk_...", "SPOTIFY_CLIENT_SECRET": "..."}
# Найбезпечніше — задати змінні середовища (вони НЕ синхронізуються в OneDrive).
# LIN_SECRETS_PATH дозволяє тримати secrets.json ПОЗА OneDrive (рекомендовано).
SECRETS_PATH = os.environ.get("LIN_SECRETS_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "secrets.json")
try:
    with open(SECRETS_PATH, encoding="utf-8") as _sf:
        _SECRETS = json.load(_sf)
        if not isinstance(_SECRETS, dict):
            _SECRETS = {}
except Exception:
    _SECRETS = {}


def _secret(name: str, default: str = "") -> str:
    """Секрет із env → secrets.json → default (порожні значення ігноруються)."""
    return os.environ.get(name) or str(_SECRETS.get(name) or "") or default


GROQ_API_KEY = _secret("GROQ_API_KEY")
# Обидві — міркувальні, параметри стримування див. _model_kwargs()
# Імена змінних історичні (колись був лише Groq) — тепер це просто основна
# й резервна модель, провайдер задається префіксом.
# Навмисно РІЗНІ провайдери: якщо один вимкне модель, асистент не замовкне.
# Ліміти безкоштовних тарифів, зняті 2026-08-15 (заголовки Groq + AI Studio):
#   Groq gpt-oss-120b     — 1000 запитів/добу
#   Groq llama-3.1-8b     — 14400 запитів/добу
#   Gemini 3.1 Flash Lite — 500 запитів/добу, 15 за хвилину
#   Gemini 3.7 / 3.5 Flash— ЛИШЕ 20 запитів/добу, для щоденного вжитку не годиться
#   Gemini Pro            — 429 одразу, недоступні без оплати
GROQ_PRIMARY_MODEL  = "openai/gpt-oss-120b"        # Groq, 1000/добу, reasoning medium
GROQ_FALLBACK_MODEL = "gemini:gemini-3.1-flash-lite"  # інший провайдер, 500/добу
# qwen/qwen3.6-27b у резерв НЕ беремо: вигадує власні теги —
# [Action:...], [DODATOK:...], [Pause Spotify] — Рафаель їх не розпізнає.
WAKE_WORDS = {"лін", "лин", "lin", "linz", "lynn", "ліна", "лінь",
              "lean", "lien", "lane", "line", "лінc", "lens",
              # Рафаель — нове ім'я (лишаємо «лін» як псевдонім для надійного STT)
              "рафаель", "рафаэль", "рафаїл", "рафаел", "рафа", "рафік",
              "рафаелю", "рафаеля", "raphael", "rafael", "рафаелька"}
VOICE       = "uk-UA-PolinaNeural"
VOICE_RATE  = "+25%"   # динамічно оновлюється через _adjust_voice_rate()
VOICE_PITCH = "+3Hz"   # трохи вищий тон — менш роботоподібний
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "lin.log")
SCREENSHOT_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
CHROME_PATH    = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Якщо True — Лін запитає підтвердження перед відкриттям програм/браузера
CONFIRM_ACTIONS = True

# Місто для погоди (можна написати по-англійськи або по-українськи)
WEATHER_CITY = "Vilnius"

# ── Spotify Web API (для перемикання пристроїв) ──────────────────────────────
# Щоб отримати ключі: developer.spotify.com/dashboard → Create App
# Redirect URI: http://127.0.0.1:8888/callback
SPOTIFY_CLIENT_ID     = _secret("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _secret("SPOTIFY_CLIENT_SECRET")

# ── Slack ─────────────────────────────────────────────────────────────────────
# Отримати: api.slack.com/apps → Create App → OAuth & Permissions
# Scopes (User Token): channels:history, channels:read, im:history, im:read,
#                      groups:history, groups:read, search:read, users:read
SLACK_TOKEN = _secret("SLACK_TOKEN")   # xoxp-... або xoxb-...

# ── Gmail ─────────────────────────────────────────────────────────────────────
# Отримати: console.cloud.google.com → APIs → Gmail API → Credentials → OAuth 2.0
# Зберегти credentials.json в папку Lin
GMAIL_CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmail_credentials.json")
GMAIL_TOKEN_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmail_token.json")
# Один токен на всі Google-сервіси: пошта + чернетки + календар.
# gmail.modify включає читання і додатково дозволяє мітки, прочитано/непрочитано
# та архівування. Назавжди видаляти листи він НЕ дозволяє, і це навмисно.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",         # читати + мітки + прочитано
    "https://www.googleapis.com/auth/gmail.compose",        # створювати чернетки/відправляти
    "https://www.googleapis.com/auth/gmail.settings.basic", # фільтри Gmail (серверне сортування)
    "https://www.googleapis.com/auth/calendar.events",      # події календаря
]

# ── Кілька поштових скриньок ──────────────────────────────────────────────────
# Перша — основна (вже авторизована, її токен також для Календаря).
# Щоб додати ще — авторизуй другий токен і додай рядок. label можна перейменувати.
# box: який набір правил застосовує mail_triage. «основна» це життя (гроші,
# безпека, робота, доставка), «офіційна» це пошук роботи й установи.
GMAIL_ACCOUNTS = [
    {"label": "основна", "box": "основна",  "token": GMAIL_TOKEN_PATH},
    {"label": "друга",   "box": "офіційна", "token": os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmail_token2.json")},
]

# Автосортування вхідної пошти: вішати мітки Трекер/* і прибирати шум із вхідних
# на кожній перевірці монітора. Вимикається одним прапорцем.
MAIL_TRIAGE_ENABLED = True

# ── Vision (бачення екрану) ───────────────────────────────────────────────────
VISION_MODEL = "gemini:gemini-3.1-flash-lite"   # на Groq зору немає; lite бо 500/добу

# ── Моніторинг (проактивні сповіщення) ────────────────────────────────────────
MONITOR_INTERVAL = 120   # секунд між перевіркою пошти/слаку
_MONITOR = {"gmail": False, "slack": False}   # які монітори увімкнені
MONITOR_GMAIL_AUTOSTART = True   # вмикати монітор пошти одразу на старті
_seen_gmail_ids: dict = {}   # вже оголошені листи; dict, а не set, бо треба порядок вставки
_seen_slack_ts:  float = 0.0   # час останнього оголошеного повідомлення Slack
_monitor_primed = {"gmail": False, "slack": False}   # перший прохід — без спаму

# ── Моніторинг СИСТЕМИ (навантаження + збої заліза/драйверів) ─────────────────
SYS_MONITOR_ENABLED  = True   # стежити за системою з самого старту
SYS_MONITOR_INTERVAL = 30     # секунд між перевірками ресурсів
SYS_EVENT_EVERY      = 10     # перевіряти журнал подій кожні N циклів (10×30с = 5 хв)
SYS_ALERT_COOLDOWN   = 600    # секунд між повторними сповіщеннями однієї категорії
SYS_CPU_STREAK       = 3      # скільки разів поспіль CPU має бути високим (3×30с = 90с)
SYS_THRESHOLDS = {
    "cpu":          92,   # % (стійко)
    "ram":          90,   # %
    "disk_free_gb": 5,    # ГБ вільно на C:
    "temp":         85,   # °C (якщо датчик доступний)
    "battery_low":  15,   # % заряду
}
# Джерела подій Windows, варті уваги (драйвери, диск, живлення, залізо)
SYS_EVENT_SOURCES = (
    "whea-logger",      # апаратні помилки (критично)
    "kernel-power",     # раптове вимкнення (Event 41)
    "kernel-pnp",       # проблеми драйверів
    "disk", "ntfs", "volmgr", "volsnap",   # диск
    "nvlddmkm", "amdkmdag", "igfx", "display",  # GPU драйвери
    "bugcheck",         # BSOD
)
_sys_alert_last: dict = {}     # категорія → monotonic час останнього сповіщення
_cpu_high_streak = 0           # лічильник високого CPU поспіль
_last_event_check = None       # datetime останньої перевірки журналу

# ── Push-to-talk (активація по гарячій клавіші) ───────────────────────────────
PUSH_TO_TALK      = False          # True — слухати ТІЛЬКИ після натискання клавіші
PTT_HOTKEY        = "ctrl+alt+m"   # запасна клавіша активації (основна — тап, див. TAP_*)
PTT_TOGGLE_HOTKEY = "ctrl+alt+l"   # увімкнути/вимкнути режим кнопки
BRAIN_HOTKEY      = "ctrl+alt+b"   # диктування одразу у Вхідні другого мозку
BRAIN_DICTATE_LIMIT = 60           # скільки секунд максимум триває один фрагмент диктовки
BRAIN_PAUSE_THRESHOLD = 2.5        # пауза (сек), після якої фрагмент вважається завершеним;
                                   # звичайні 0.9 рвали абзац на шматки посеред речень
_ptt_event = threading.Event()     # ставиться коли натиснуто клавішу активації

# ── Watchdog (авто-перезапуск) ────────────────────────────────────────────────
STOP_MARKER = os.path.join(SCRIPT_DIR, ".lin_stop")   # створюється при свідомому виході
# Рафаель пропрацював ALIVE_AFTER секунд → старт вдався. start.bat за цим файлом
# відрізняє падіння на старті (зламана установка) від випадкового збою.
ALIVE_MARKER = os.path.join(SCRIPT_DIR, ".lin_alive")
ALIVE_AFTER  = 120

# ============================================================
#  РЕЖИМИ
# ============================================================
# normal    — слухає тільки після "Лін"
# chat      — відповідає на будь-яку фразу без wake word
# dictation — все що кажеш → вставляється в активне вікно
MODE   = "normal"
LIN_UI = None   # встановлюється в main()

_last_spoken = ""   # остання фраза Лін — для команди "повтори"

# Фрази «повтори»: voice_rules.REPEAT_PHRASES / is_repeat_request.

CHAT_MODE_TRIGGERS   = {
    "режим розмови", "chat mode", "розмовний режим", "говори зі мною", "speak mode",
    "почни розмову", "режим чату", "чат режим", "просто говори",
}
NORMAL_MODE_TRIGGERS = {
    "звичайний режим", "нормальний режим", "вийди з розмови", "стоп режим",
    "normal mode", "замовкни", "вийди з чату", "стоп чат", "зупини розмову",
    "завершити розмову", "виходь з режиму", "повернись в звичайний",
    "повернись до звичайного", "вимкни режим розмови",
}
DICTATION_TRIGGERS = {
    "режим диктування", "диктуй", "dictation", "диктування",
    "режим введення", "починай диктувати", "стартуй диктування",
}
# Вихід з диктування і голосова пунктуація: voice_rules.is_dictation_exit
# та voice_rules.apply_voice_punctuation (цілими словами, з тестами).

# ── Динамічна швидкість голосу ────────────────────────────────────────────────
_VOICE_RATE_VALUE = 25   # поточний відсоток (ціле число)
_VOICE_RATE_MIN   = -20
_VOICE_RATE_MAX   =  60
_VOICE_RATE_STEP  =  10

FASTER_WORDS = {"швидше", "говори швидше", "faster", "прискорити", "швидший темп"}
SLOWER_WORDS = {"повільніше", "говори повільніше", "slower", "уповільнити", "повільний темп"}
RESET_SPEED_WORDS = {"нормальна швидкість", "звичайна швидкість", "нормальний темп",
                     "звичайний темп", "скинь швидкість"}

# ============================================================
#  НОТАТКИ / ПЛАНИ
# ============================================================
NOTES_PATH = os.path.join(SCRIPT_DIR, "notes.json")


_notes_cache: list = []
_notes_cache_time: float = 0.0
_NOTES_TTL = 5.0   # секунд між повторними читаннями файлу

def _load_notes() -> list:
    global _notes_cache, _notes_cache_time
    now = time.monotonic()
    if now - _notes_cache_time < _NOTES_TTL and _notes_cache is not None:
        return _notes_cache
    if os.path.exists(NOTES_PATH):
        with open(NOTES_PATH, encoding="utf-8") as f:
            _notes_cache = json.load(f)
    else:
        _notes_cache = []
    _notes_cache_time = now
    return _notes_cache


def _write_json_atomic(path: str, data):
    """Запис через тимчасовий файл: якщо процес впаде посеред запису (а watchdog
    його одразу підніме), на диску лишиться стара версія, а не обрізаний JSON."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _save_notes(notes: list):
    global _notes_cache, _notes_cache_time
    _write_json_atomic(NOTES_PATH, notes)
    _notes_cache = notes          # одразу оновлюємо кеш
    _notes_cache_time = time.monotonic()


# Розбір часу нагадування живе в voice_rules (там тести): "21:00", "30",
# "2026-09-26 09:00". Неможливий час чи минула дата дають None, а не виняток.
parse_remind_time = vr.parse_remind_time


def note_add(text: str, remind_minutes: int = 0, remind_at_str: str = "") -> str:
    """Додає нотатку/план. remind_minutes=0 — без нагадування."""
    notes = _load_notes()
    new_id = max((n["id"] for n in notes), default=0) + 1
    remind_at = None
    if remind_at_str:
        remind_at = parse_remind_time(remind_at_str)
        if remind_at is None:
            # Раніше тут мовчки записувалась звичайна нотатка, а Рафаель казав
            # «Записала», і ти думав, що нагадування буде. Тепер чесно.
            log.warning(f"Нагадування: не зрозуміла час '{remind_at_str}' для «{text}»")
            return (f"Не зрозуміла, коли нагадати про «{text}». Скажи, наприклад, "
                    "о 21:00, через 30 хвилин або завтра о 9.")
    elif remind_minutes > 0:
        remind_at = (datetime.now() + timedelta(minutes=remind_minutes)).strftime("%Y-%m-%d %H:%M")
    note = {
        "id": new_id,
        "text": text,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "remind_at": remind_at,
        "done": False,
    }
    notes.append(note)
    _save_notes(notes)
    log.info(f"Нотатка додана #{new_id}: {text}" + (f" (нагадування: {remind_at})" if remind_at else ""))
    if remind_at:
        return f"Записала: «{text}». Нагадаю {vr.describe_remind(remind_at)}."
    return f"Записала: «{text}»."


def note_list() -> str:
    """Повертає список активних нотаток."""
    notes = [n for n in _load_notes() if not n["done"]]
    if not notes:
        return "Нотаток немає."
    lines = []
    for n in notes:
        remind = f" (нагадування {vr.describe_remind(n['remind_at'])})" if n.get("remind_at") else ""
        lines.append(f"{n['id']}. {n['text']}{remind}")
    return "Твої плани: " + ". ".join(lines)


def note_done(note_id: int) -> str:
    """Відмічає нотатку як виконану."""
    notes = _load_notes()
    for n in notes:
        if n["id"] == note_id:
            n["done"] = True
            _save_notes(notes)
            log.info(f"Нотатка #{note_id} виконана")
            return f"Відмітила план «{n['text']}» як виконаний."
    return f"Нотатку #{note_id} не знайдено."


def note_delete(note_id: int) -> str:
    """Видаляє нотатку."""
    notes = _load_notes()
    before = len(notes)
    notes = [n for n in notes if n["id"] != note_id]
    if len(notes) < before:
        _save_notes(notes)
        log.info(f"Нотатка #{note_id} видалена")
        return f"Видалила нотатку #{note_id}."
    return f"Нотатку #{note_id} не знайдено."


def note_clear(which: str = "all") -> str:
    """
    Очищає плани. which:
      'all'  — видалити всі;
      'done' — видалити тільки виконані;
      'найстаріший за N днів' не підтримується тут.
    """
    notes = _load_notes()
    before = len(notes)
    if before == 0:
        return "Планів і так немає."
    if which == "done":
        kept = [n for n in notes if not n.get("done")]
        removed = before - len(kept)
        if removed == 0:
            return "Виконаних планів немає."
        _save_notes(kept)
        log.info(f"Очищено виконаних планів: {removed}")
        return f"Прибрала {vr.count(removed, 'виконаний план', 'виконані плани', 'виконаних планів')}."
    # all
    _save_notes([])
    log.info(f"Очищено всі плани: {before}")
    return f"Очистила всі плани, прибрала {vr.count(before, 'план', 'плани', 'планів')}."


def reminder_loop():
    """Фоновий поток — перевіряє нагадування кожні 30 секунд."""
    log.info("Reminder loop запущено")
    while True:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            notes = _load_notes()
            fired_ids = []
            for n in notes:
                if n.get("remind_at") and not n["done"] and n["remind_at"] <= now_str:
                    log.info(f"Нагадування спрацювало (видаляю план): {n['text']}")
                    speak(f"Нагадую: {n['text']}")
                    fired_ids.append(n["id"])
            if fired_ids:
                # Видаляємо плани що спрацювали — щоб не повторювались
                notes = [n for n in notes if n["id"] not in fired_ids]
                _save_notes(notes)
        except Exception as e:
            log.error(f"Reminder loop помилка: {e}")
        time.sleep(30)


# ============================================================
#  ДРУГИЙ МОЗОК (сховище Obsidian)
# ============================================================
# Пишемо прямо у файл, а не через REST API плагіна: так працює навіть коли
# Obsidian закритий, і ключ від API не треба тримати в Ліні.
# Шлях через %USERPROFILE%, без імені користувача; інший шлях можна задати
# ключем BRAIN_VAULT у config.json
BRAIN_VAULT = os.path.expandvars(r"%USERPROFILE%\OneDrive\Документы\memory\memory")
BRAIN_INBOX = os.path.join(BRAIN_VAULT, "Вхідні.md")

# Куди саме класти надиктоване. Ключ — як це називає Влад голосом.
BRAIN_TARGETS = {
    "вхідні": "Вхідні.md",
    "плани":  "Плани.md",
    "ідеї":   "Ідеї.md",
}

_BRAIN_HEADER = (
    "---\n"
    "type: inbox\n"
    "tags: [вхідні]\n"
    "updated: {date}\n"
    "summary: Надиктоване голосом через Рафаеля. Розбирається потім у нотатки.\n"
    "---\n\n"
    "# {title}\n\n"
    "Сюди Рафаель дописує те, що надиктовано на ходу. Правил немає, формату немає.\n\n"
)


def brain_capture(text: str, target: str = "вхідні") -> str:
    """Дописує рядок у потрібний файл сховища: вхідні, плани або ідеї."""
    text = (text or "").strip()
    if not text:
        return "А що саме записати? Я нічого не розчула."
    if not os.path.isdir(BRAIN_VAULT):
        log.error(f"Brain: сховища немає за шляхом {BRAIN_VAULT}")
        return "Не знайшла сховище на диску. Записати нікуди."

    key = (target or "вхідні").strip().lower()
    fname = BRAIN_TARGETS.get(key, BRAIN_TARGETS["вхідні"])
    path = os.path.join(BRAIN_VAULT, fname)
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_BRAIN_HEADER.format(date=datetime.now().strftime("%Y-%m-%d"),
                                             title=fname[:-3]))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- **{datetime.now().strftime('%Y-%m-%d %H:%M')}** — {text}\n")
        log.info(f"Brain capture [{fname}]: {text[:80]}")
        where = {"плани": "у плани", "ідеї": "в ідеї"}.get(key, "у вхідні")
        return f"Записала {where}: «{text}»."
    except Exception as e:
        log.error(f"Brain capture помилка: {e}")
        return "Не вийшло записати, файл не піддався."


_brain_index_cache = None
_brain_index_time  = 0.0
_BRAIN_INDEX_TTL   = 300          # перечитувати список нотаток раз на 5 хв


def _brain_notes() -> list:
    """Список нотаток сховища: назва, аліаси, summary. Кешується."""
    global _brain_index_cache, _brain_index_time
    now = time.monotonic()
    if _brain_index_cache is not None and now - _brain_index_time < _BRAIN_INDEX_TTL:
        return _brain_index_cache
    items = []
    try:
        for fn in sorted(os.listdir(BRAIN_VAULT)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(BRAIN_VAULT, fn)
            summary, aliases = "", ""
            try:
                with open(path, encoding="utf-8") as f:
                    head = f.read(1500)
            except Exception:
                continue
            for line in head.split("\n")[:15]:
                low = line.lower()
                if low.startswith("summary:"):
                    summary = line.split(":", 1)[1].strip()
                elif low.startswith("aliases:"):
                    aliases = line.split(":", 1)[1].strip(" []")
            items.append({"name": fn[:-3], "summary": summary,
                          "aliases": aliases, "path": path})
    except Exception as e:
        log.error(f"Brain index: {e}")
    _brain_index_cache, _brain_index_time = items, now
    return items


# Питальні й службові слова: довші за 3 літери, але сенсу для пошуку не несуть.
_BRAIN_STOP = {
    "який", "яка", "яке", "які", "коли", "куди", "чому", "хто", "що",
    "мене", "мені", "мого", "моя", "мої", "твій", "цей", "цього", "там", "тут",
    "треба", "можна", "скажи", "розкажи", "покажи", "нагадай", "такий", "така",
    "таке", "було", "буде", "щось", "чогось", "лежать", "лежить", "зараз",
}


def _brain_find(query: str, limit: int = 3) -> list:
    """
    Шукає нотатки за назвою, аліасами, summary, а якщо не вийшло — по тілу.
    Порівнюємо ОСНОВИ слів, бо українська відмінює: «магазину» має знайти «магазин».
    """
    q = (query or "").lower().strip()
    words = [w for w in re.split(r"\W+", q) if len(w) > 3 and w not in _BRAIN_STOP]
    stems = [w[:5] for w in words]
    if not stems:
        return []

    # Прохід 1: тільки назва, аліаси, summary. Це найточніше й не залежить
    # від розміру нотатки.
    meta = []
    for it in _brain_notes():
        hay = f"{it['name']} {it['aliases']} {it['summary']}".lower()
        score = (10 if q in hay else 0) + sum(4 for st in stems if st in hay)
        if score:
            meta.append((score, it))
    meta.sort(key=lambda x: -x[0])

    # Впевнений збіг це або точна фраза, або більшість основ запиту. Одна
    # випадкова основа впевненістю не є.
    strong = bool(meta) and meta[0][0] >= max(10, 4 * max(1, len(stems) - 1))
    if strong:
        return [it for _, it in meta[:limit]]

    # Прохід 2: по тілу. Рахуємо кількість входжень (з обмеженням), інакше
    # довгі нотатки-індекси перемагають лише через свій розмір.
    #
    # Раніше цей прохід запускався ЛИШЕ коли перший не дав нічого. Через це
    # один слабкий випадковий збіг у метаданих блокував пошук по тілах:
    # запит з імʼям і прізвищем колеги знаходив нотатку Agentic UX (бо в її
    # summary є те саме імʼя) і ніколи не доходив до «Люди», де про колегу
    # власне й написано. Тепер слабкий результат першого проходу не зупиняє,
    # а лише додається до другого.
    body_scored = []
    for it in _brain_notes():
        try:
            with open(it["path"], encoding="utf-8") as f:
                body = f.read().lower()
        except Exception:
            continue
        score = sum(min(body.count(st), 5) for st in stems)
        if score:
            body_scored.append((score, it))

    merged = {}
    for score, it in body_scored:
        merged[it["name"]] = (score, it)
    for score, it in meta:                      # метадані цінніші за тіло
        prev = merged.get(it["name"], (0, it))[0]
        merged[it["name"]] = (prev + score, it)
    ranked = sorted(merged.values(), key=lambda x: -x[0])
    return [it for _, it in ranked[:limit]]


def brain_read(name: str) -> str:
    """Коротко: про що нотатка. Без звертання до моделі."""
    found = _brain_find(name, limit=1)
    if not found:
        return f"Не знайшла нотатки про {name}."
    it = found[0]
    return f"{it['name']}. {it['summary']}" if it["summary"] else f"Нотатка {it['name']} є, але без опису."


def brain_ask(question: str) -> None:
    """Відповідає на питання, спираючись на нотатки сховища."""
    def _run():
        try:
            found = _brain_find(question)
            if not found:
                speak("Не знайшла нічого в мозку по цьому.")
                return
            parts = []
            for it in found:
                try:
                    with open(it["path"], encoding="utf-8") as f:
                        parts.append(f"### {it['name']}\n{f.read()[:2500]}")
                except Exception:
                    continue
            if not parts:
                speak("Нотатки знайшла, але прочитати не змогла.")
                return
            log.info(f"Brain ask '{question}': {[i['name'] for i in found]}")
            resp = llm_chat(
                GROQ_PRIMARY_MODEL,
                # Задача сформульована як «перекажи, що є по темі», а НЕ як
                # «відповідай на питання». Причина: запит приходить із
                # розпізнавання мови й часто є уламком («по контрактах»).
                # На такому уламку модель не знаходила буквальної відповіді
                # й казала «в нотатках нічого немає», хоча тримала перед собою
                # рівно ті нотатки, які треба.
                messages=[{"role": "user", "content": (
                    # Без цього уточнення модель плутала Влада з колегою-тезком,
                    # який згадується в нотатках, і склеювала їх в одну людину.
                    "«Влад» це Владислав Собакар, власник цих нотаток. Усі інші люди, "
                    "згадані в нотатках, це його колеги й знайомі, а не він сам. "
                    "Ніколи не приписуй йому чужого прізвища.\n\n"
                    f"Влад питає голосом про: «{question}». Запит розпізнаний з мовлення, "
                    "тому може бути обірваним або з помилками, не чіпляйся до формулювання.\n\n"
                    "Нижче нотатки з його сховища, знайдені за цією темою. Стисло, 2-3 речення "
                    "українською, перекажи головне саме по цій темі: у якому стані справа, "
                    "що вирішено, що далі. Спирайся ТІЛЬКИ на нотатки, не вигадуй.\n"
                    "Відповідай «у нотатках про це нічого немає» лише тоді, коли теми там "
                    "справді немає.\n\n" + "\n\n".join(parts)
                )}],
                max_tokens=220,
                temperature=0.3,
            )
            answer = resp.choices[0].message.content.strip()
            log.info(f"Brain answer: {answer[:120]}")
            speak(answer)
        except Exception as e:
            log.error(f"Brain ask помилка: {e}", exc_info=True)
            speak("Не вийшло подивитись у мозок.")

    threading.Thread(target=_run, daemon=True).start()


# ============================================================
#  ПАМ'ЯТЬ МІЖ СЕСІЯМИ
# ============================================================
MEMORY_PATH = os.path.join(SCRIPT_DIR, "memory.json")


def load_memory() -> dict:
    if os.path.exists(MEMORY_PATH):
        with open(MEMORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"user_name": "", "last_session": "", "facts": [], "session_count": 0}


def save_memory(data: dict):
    _write_json_atomic(MEMORY_PATH, data)


def update_memory_after_session(exchanges: list):
    """Зберігає короткий підсумок сесії через Groq."""
    # Без системного промпту: раніше він (8 тис. символів) ішов у підсумок
    # разом з розмовою, коли обмінів було мало
    exchanges = [m for m in exchanges if m.get("role") in ("user", "assistant")]
    if len(exchanges) < 3:
        return
    try:
        mem = load_memory()
        summary_prompt = (
            "Зроби короткий підсумок (1-2 речення) цієї розмови для майбутньої пам'яті. "
            "Вкажи що робив користувач, про що говорили, важливі факти. "
            "Відповідь — тільки підсумок без пояснень:\n\n"
            + "\n".join([f"{m['role']}: {m['content']}" for m in exchanges[-10:]])
        )
        resp = llm_chat(
            GROQ_PRIMARY_MODEL,
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=300,          # міркувальні моделі з'їдають частину ліміту
        )
        summary = resp.choices[0].message.content.strip()
        mem["last_session"] = summary
        mem["session_count"] = mem.get("session_count", 0) + 1
        save_memory(mem)
        log.info(f"Пам'ять збережена: {summary}")
    except Exception as e:
        log.error(f"Помилка збереження пам'яті: {e}")


def build_system_prompt() -> str:
    """Будує системний промпт з урахуванням пам'яті."""
    mem = load_memory()
    memory_block = ""
    if mem.get("last_session"):
        memory_block = f"Попередня сесія: {mem['last_session']} "
    if mem.get("user_name"):
        memory_block += f"Ім'я користувача: {mem['user_name']}. "
    if mem.get("facts"):
        memory_block += "Факти про користувача: " + "; ".join(mem["facts"][-5:]) + ". "

    notes_reminder = ""
    pending = [n for n in _load_notes() if not n["done"]]
    if pending:
        notes_reminder = f"Активні плани користувача: {', '.join([n['text'] for n in pending[:3]])}. "

    # Індекс другого мозку: у промті лише НАЗВИ нотаток, вміст читається на вимогу.
    # Пхати сюди самі нотатки не можна — це десятки тисяч символів у кожному запиті.
    brain_block = ""
    try:
        names = [n["name"] for n in _brain_notes()]
        if names:
            brain_block = (
                "ДРУГИЙ МОЗОК: у Влада є сховище нотаток. Наявні нотатки: "
                + ", ".join(names) + ". "
                "Якщо питання стосується його роботи, проєктів, планів, людей, "
                "тікетів чи домовленостей — НЕ ВИГАДУЙ, а виклич brain_ask:питання. "
                "Ти НЕ бачиш вміст нотаток, поки не викличеш дію — тому не описуй "
                "їх з голови. Про що нотатка, ти знаєш ЛИШЕ з її назви. "
                "Правильно: коротке «зараз гляну» плюс тег. Неправильно: переказ "
                "змісту, якого ти не читав. "
            )
    except Exception as e:
        log.debug(f"brain_block пропущено: {e}")

    return (
        "Ти — Рафаель (можна Рафа). Голосовий асистент Влада. Говориш тільки українською. "
        "Ти як близька знайома — коротко, по-людськи, без жодної офіційщини. "
        "Відповідь — одне речення, максимум два. Без довгих пояснень. "
        "СТИЛЬ: говориш природно, можеш казати 'ну', 'хм', 'ага', 'та ладно', 'окей'. "
        "Ніяких 'Звичайно!', 'Відмінно!', 'Я розумію'. Це звучить як робот. "
        "ГОЛОВНЕ: НЕ ПОВТОРЮЙ ЩО СКАЗАВ ВЛАД — просто відповідай по суті. "
        "Якщо не знаєш — кажи 'не знаю'. Якщо питання дурне — скажи прямо. "
        "ЧЕСНІСТЬ — АБСОЛЮТНЕ ПРАВИЛО: "
        "якщо відкриваєш щось — кажи 'відкриваю', не 'відкрила'. "
        "Якщо дія спрацювала — підтверди. Якщо ні — скажи чесно. "
        "Не вигадуй що щось зроблено якщо ти тільки ініціюєш це. "
        "РОЗУМІННЯ: команди приходять голосом, тому текст буває з помилками розпізнавання, "
        "суржиком чи російськими словами — лови НАМІР, а не чіпляйся до формулювання. "
        "Якщо майже зрозуміло — дій; якщо справді незрозуміло — коротко перепитай, не вигадуй. "
        "ДІЇ: коли виконуєш — додай тег [ACTION:тип:параметр] в кінці відповіді. "
        "ЛИШЕ ОДИН тег на відповідь: виконується перший, решта мовчки гине. "
        "НЕ дублюй назву дії звичайним текстом перед тегом — її зачитають уголос. "
        "Якщо потрібні дві дії — зроби першу, про другу спитай. "
        "open_app:назва | search_web:запит | open_youtube:запит | open_url:посилання | "
        "open_folder:шлях | get_time: | volume_up: | volume_down: | volume_mute: | "
        "screenshot: | type_text:текст | hotkey:комбінація | "
        "close_window: | minimize_all: | focus_window:назва | "
        "system_lock: | system_sleep: | system_restart: | system_shutdown_pc: | "
        "system_cancel_shutdown: — скасувати заплановане вимкнення | kill_process:назва | "
        "spotify_play:запит | spotify_pause: | spotify_next: | spotify_prev: | "
        "spotify_volume_up: | spotify_volume_down: | spotify_volume:N (0-100) | "
        "spotify_shuffle:on/off — перемішування | spotify_repeat:track/context/off — повтор | "
        "spotify_playlists: — список плейлистів | spotify_play_playlist:назва — відтворити плейлист | "
        "spotify_queue: — черга треків | spotify_playlist_tracks:назва — треки з плейлисту | "
        "spotify_add_queue:трек — додати в чергу | "
        "spotify_device:телефон або пк | spotify_devices: | "
        "note_add:текст | note_remind:текст|час | note_list: | note_done:номер | note_delete:номер | "
        "note_clear: — видалити ВСІ плани | note_clear:done — видалити виконані | "
        "brain_add:текст — записати думку у другий мозок (сховище Obsidian) | "
        "brain_plan:текст — записати у ПЛАНИ сховища | "
        "brain_idea:текст — записати в ІДЕЇ сховища | "
        "brain_ask:питання — знайти відповідь у нотатках сховища й відповісти | "
        "brain_read:назва — коротко про що конкретна нотатка | "
        "dictate_type: — почати диктовку і надрукувати її туди, де стоїть курсор | "
        "dictate_last: — знову покласти останню диктовку в буфер обміну | "
        "dictate_to_brain: — зберегти останню диктовку у Вхідні сховища | "
        "focus_start:хвилини | focus_stop: | memory_save_name:ім'я | memory_add_fact:факт | "
        "web_search:запит — пошук в інеті, відповідь ГОЛОСОМ (за замовчуванням для 'знайди/пошукай/що таке/скільки коштує') | "
        "search_web:запит — відкрити вкладку Chrome (ЛИШЕ коли явно просять 'браузер/вкладку/в хромі/на сайті') | "
        "ask_claude:текст | claude_session:N | claude_list: | claude_read:тема | "
        "spotify_current: — що зараз грає | "
        "spotify_recent: — нещодавно слухав | "
        "spotify_liked: — лайкнуті треки | "
        "notepad_write:текст — відкрити Блокнот і написати текст | "
        "weather: — погода у місті за замовчуванням | "
        "weather:місто — погода у конкретному місті | "
        "system_info: — CPU, RAM, диск, батарея | "
        "timer:секунди — таймер (600 = 10 хв, 30 = 30 сек) | "
        "timer_stop: — скасувати всі таймери | "
        "clipboard_read: — прочитати буфер обміну | "
        "clipboard_save: — зберегти буфер в нотатки | "
        "clipboard_copy:текст — скопіювати текст в буфер | "
        "voice_faster: | voice_slower: | voice_reset: — швидкість голосу | "
        "window_snap:left/right/max/min — snap вікна | "
        "currency:сума:від:до — конвертація (100:USD:UAH) | "
        "slack_unread: — непрочитані в Slack | slack_dm: — приватні DM | "
        "slack_mentions: — згадки | slack_channel:назва — канал | "
        "gmail_unread: — непрочитані листи | gmail_latest: — останні листи | "
        "gmail_search:запит — пошук листів | gmail_read_full:запит — прочитати лист повністю | "
        "gmail_reply:запит|текст — чернетка відповіді | "
        "calendar_today: | calendar_tomorrow: | calendar_week: — події | "
        "calendar_create:Назва|YYYY-MM-DD HH:MM|хвилини — створити подію | "
        "screen_look: — подивитись що на екрані | screen_look:питання — відповісти про екран | "
        "monitor_on:gmail/slack/system/all — стежити | monitor_off:... — не стежити | "
        "system_health: — стан системи + помилки журналу | "
        "shutdown:. "
        "Нагадування: 'о 21:00' → note_remind:текст|21:00; 'через 30 хв' → note_remind:текст|30; "
        "'завтра о 9', 'у пʼятницю о 10' → note_remind:текст|РРРР-ММ-ДД ГГ:ХХ (дату порахуй від сьогоднішньої). "
        "Таймер: 'таймер 10 хвилин' → timer:600; 'таймер 30 секунд' → timer:30. "
        "Spotify: 'що грає' — spotify_current:; 'що слухав' — spotify_recent:; 'лайкнуті' — spotify_liked:. "
        "Гучність Spotify: 'гучніше/голосніше в spotify' → spotify_volume_up:; "
        "'тихіше/тише в spotify' → spotify_volume_down:; 'spotify на 50%' → spotify_volume:50. "
        "Shuffle: 'увімкни перемішування' → spotify_shuffle:on; 'вимкни шафл' → spotify_shuffle:off; 'перемішай' → spotify_shuffle:toggle. "
        "Повтор: 'повторюй трек' → spotify_repeat:track; 'повторюй плейлист' → spotify_repeat:context; 'вимкни повтор' → spotify_repeat:off. "
        "Плейлисти: 'покажи плейлисти' → spotify_playlists:; 'ввімкни плейлист Chill' → spotify_play_playlist:Chill. "
        "Черга: 'що далі' → spotify_queue:; 'покажи чергу' → spotify_queue:; "
        "'треки в плейлисті' → spotify_playlist_tracks:; 'треки в плейлисті Chill' → spotify_playlist_tracks:Chill. "
        "Черга: 'добав в чергу Bohemian Rhapsody' → spotify_add_queue:Bohemian Rhapsody. "
        "Slack: 'що в слаку' / 'непрочитані слак' → slack_unread:; "
        "'DM в слаку' / 'приватні' → slack_dm:; 'згадки' → slack_mentions:; "
        "'канал general' → slack_channel:general. "
        "Gmail: 'що в пошті' / 'непрочитані листи' → gmail_unread:; "
        "'останні листи' → gmail_latest:; 'листи від Влада' → gmail_search:from:Vlad. "
        "'прочитай останній лист' → gmail_read_full:; 'прочитай лист від Google' → gmail_read_full:from:Google. "
        "'відповідай йому що буду о 5' → gmail_reply:|Привіт, буду о 5. "
        "Календар: 'що в мене сьогодні' → calendar_today:; 'що завтра' → calendar_tomorrow:; "
        "'плани на тиждень' → calendar_week:. Створення події: визнач АБСОЛЮТНУ дату-час з контексту "
        "(сьогодні/завтра + час) і дай calendar_create:Назва|РРРР-ММ-ДД ГГ:ХХ|хвилини. "
        "Напр. 'зустріч завтра о 15' → calendar_create:Зустріч|<завтрашня дата> 15:00|60. "
        "Екран: 'що на екрані' / 'подивись на екран' → screen_look:; "
        "'що тут написано' / 'переклади екран' / 'що це за помилка' → screen_look:питання. "
        "Моніторинг: 'стеж за поштою' → monitor_on:gmail; 'стеж за слаком' → monitor_on:slack; "
        "'стеж за системою' → monitor_on:system; 'стеж за всім' → monitor_on:all; 'не стеж за поштою' → monitor_off:gmail. "
        "Система: 'як справи з системою' / 'перевір систему' → system_health:; "
        "'скільки RAM' / 'навантаження' → system_info:. "
        "Погода: 'яка погода' — weather:; 'погода в Берліні' — weather:Berlin. "
        "Система: 'скільки RAM', 'навантаження', 'батарея' — system_info:. "
        "Вікна: 'вікно ліворуч' → window_snap:left; 'на весь екран' → window_snap:max. "
        "Валюта: '100 доларів в гривнях' → currency:100:USD:UAH. "
        "Блокнот: 'напиши в блокноті X' → notepad_write:X. "
        "Друк під диктовку: голе 'пиши', 'друкуй' без тексту → dictate_type: (Влад "
        "продиктує окремо). Якщо текст названий одразу — 'пиши наступне: …', "
        "'надрукуй що …' — віддай його параметром: dictate_type:сам текст. "
        "'що я диктував', 'скопіюй те, що я диктував', 'поверни диктовку' → dictate_last:. "
        "'збережи диктовку в мозок', 'то в нотатки' → dictate_to_brain:. "
        "Це коли Влад хоче надиктувати текст у поле, де стоїть курсор. "
        "Не плутати: 'напиши в блокноті X' — це notepad_write з готовим текстом, "
        "а dictate_type: не має параметра, Влад продиктує окремо. "
        "Другий мозок: 'запиши в мозок X' / 'занотуй в мозок X' / 'у вхідні X' / "
        "'кинь у сховище X' / 'запам'ятай в обсідіан X' → brain_add:X. "
        "ГОЛОВНЕ ПРАВИЛО ЗАПИСУ: усе, що Влад просить записати, йде у СХОВИЩЕ "
        "Obsidian, а не у твої внутрішні нотатки. "
        "'в плани', 'додай в плани', 'запиши в плани' → brain_plan:текст. "
        "'ідея', 'в ідеї', 'запиши ідею' → brain_idea:текст. "
        "'запиши', 'занотуй', 'в мозок', 'не забути' → brain_add:текст. "
        "note_add і note_remind — ТІЛЬКИ коли потрібне нагадування у конкретний "
        "час ('нагадай о 21:00', 'через годину'). Без часу — завжди brain_*. "
        "Слово «плани» саме по собі означає файл Плани у сховищі, а не твій список. "
        "Якщо дія не потрібна — відповідай без тегу. "
        # Змінне в самому кінці: постійна частина вище однакова в кожному
        # запиті, і провайдери, що кешують початок промпту, її перевикористають.
        # Час inject_time() допише ще далі.
        + brain_block + memory_block + notes_reminder
    ).rstrip()


# ============================================================
#  РЕЖИМ ФОКУСУ / POMODORO
# ============================================================
FOCUS_ACTIVE = False
_tts_lock  = threading.Lock()
_tts_stop  = threading.Event()   # встановити → зупинити TTS достроково
# Щоб Рафаель не чув сам себе: listen() не пише, поки щось звучить, і відкидає
# запис, під час якого почалась озвучка (фонові потоки говорять коли завгодно).
_tts_active   = threading.Event()   # зараз грає озвучка
_tts_epoch    = 0                   # лічильник озвучок, росте на кожній
_tts_last_end = 0.0                 # monotonic-час, коли озвучка востаннє замовкла
TTS_ECHO_TAIL = 0.35                # стільки секунд після озвучки ще не слухаємо (відлуння)
pygame.mixer.pre_init(44100, -16, 2, 512)
pygame.mixer.init()


def focus_start(minutes: int = 25):
    global FOCUS_ACTIVE
    FOCUS_ACTIVE = True
    log.info(f"Focus mode: {minutes} хв")

    def _timer():
        global FOCUS_ACTIVE
        for remaining in range(minutes, 0, -1):
            if not FOCUS_ACTIVE:
                return
            time.sleep(60)
        if FOCUS_ACTIVE:
            FOCUS_ACTIVE = False
            speak(f"Таймер завершено! Ти працював {minutes} хвилин. Зроби перерву на 5 хвилин, ти заслужив.")

    threading.Thread(target=_timer, daemon=True).start()
    return f"Режим фокусу на {minutes} хвилин. Продуктивної роботи!"


def focus_stop():
    global FOCUS_ACTIVE
    FOCUS_ACTIVE = False
    return "Режим фокусу зупинено."


# ============================================================
#  РАНКОВИЙ БРИФІНГ
# ============================================================
BRIEFING_HOUR = 7


def morning_briefing(greeting: bool = True):
    """
    Повний ранковий дайджест: дата + плани + погода + календар + пошта + система.
    greeting=False — без «Доброго ранку» (для виклику вдень на вимогу).
    """
    now  = datetime.now()
    days = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
    log.info("Ранковий дайджест")

    # ── 1. Дата + плани (нотатки) ────────────────────────────
    hello = "Доброго ранку! " if greeting else ""
    text = f"{hello}Сьогодні {days[now.weekday()]}, {now.strftime('%d.%m.%Y')}. "
    pending = [n for n in _load_notes() if not n["done"]]
    if pending:
        count = len(pending)
        noun  = vr.plural(count, "план", "плани", "планів")
        names = ", ".join(n["text"] for n in pending[:3])
        tail  = f" і ще {count - 3}" if count > 3 else ""
        text += f"У тебе {count} {noun}: {names}{tail}. "
    else:
        text += "Планів у нотатках немає. "
    speak(text)

    # ── 2. Погода ────────────────────────────────────────────
    try:
        speak(f"Погода: {_get_weather()}")
    except Exception as e:
        log.error(f"Дайджест погода: {e}")

    # ── 3. Календар на сьогодні ──────────────────────────────
    try:
        if _get_calendar():
            _calendar_agenda("today")
    except Exception as e:
        log.error(f"Дайджест календар: {e}")

    # ── 4. Непрочитана пошта (обидві скриньки) ───────────────
    try:
        if _gmail_accounts():
            _gmail_unread(limit=5)
    except Exception as e:
        log.error(f"Дайджест пошта: {e}")

    # ── 5. Стан системи (коротко) ────────────────────────────
    try:
        speak(_system_health_report())
    except Exception as e:
        log.error(f"Дайджест система: {e}")

    if greeting:
        speak("Гарного дня!")


def briefing_loop():
    last_date = None
    while True:
        now = datetime.now()
        if now.hour == BRIEFING_HOUR and now.date() != last_date:
            last_date = now.date()
            morning_briefing()
        time.sleep(60)

# ============================================================
#  ЛОГИ
# ============================================================
def _trim_log_if_large(path: str, keep_lines: int = 1000):
    """Обрізає лог якщо він перевищив keep_lines рядків. Викликається перед init логера."""
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > keep_lines * 1.5:   # більше ніж в 1.5 рази — обрізаємо
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_lines:])
    except Exception:
        pass

_trim_log_if_large(LOG_PATH)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Файловий хендлер: INFO, ротація 500 KB, 3 архіви → максимум ~2 MB
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_fmt)

# Консольний хендлер: WARNING, лише коли консоль справді є. Під pythonw
# stderr веде в crash.log, і туди йдуть тільки падіння, а не кожне попередження.
_log_handlers = [_file_handler]
if not _PYTHONW:
    _con_handler = logging.StreamHandler()
    _con_handler.setLevel(logging.WARNING)
    _con_handler.setFormatter(_fmt)
    _log_handlers.append(_con_handler)

logging.basicConfig(level=logging.DEBUG, handlers=_log_handlers)

# Заглушуємо шумні бібліотеки
for _noisy in ("PIL", "httpcore", "httpx", "urllib3", "spotipy"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("Лін")

def _make_groq_client(key: str):
    """Клієнт для Whisper. Таймаут короткий і без повторів SDK: за замовчуванням
    це 60 с × 3 спроби на кожну фразу, а за Whisper і так є Google і Vosk."""
    return Groq(api_key=key, timeout=15.0, max_retries=0)


client = _make_groq_client(GROQ_API_KEY)
recognizer = sr.Recognizer()
# Баланс швидкість/точність: 0.9с тиші — Лін реагує швидше, але не ріже
# повільних мовців. Якщо обриває на півслові — підніми до 1.1; якщо реагує
# повільно — опусти до 0.7.
recognizer.pause_threshold        = 0.9   # чекати 0.9с тиші перед кінцем фрази
recognizer.non_speaking_duration  = 0.5   # мінімальний час тиші для кінця
recognizer.dynamic_energy_threshold = True

SYSTEM_PROMPT = (
    "Ти — Рафаель (можна Рафа). Голосовий асистент Влада. Говориш тільки українською. "
    "Відповідь — одне речення, максимум два. Говориш природно, як людина. "
    "Не повторюй що сказав Влад — просто відповідай. Не вигадуй. "
    "Якщо виконуєш дію — [ACTION:тип:параметр]. Якщо дія не потрібна — без тегу."
)

history = [{"role": "system", "content": SYSTEM_PROMPT}]  # оновлюється динамічно в ask_lin


# ============================================================
#  ФУНКЦІЇ КЕРУВАННЯ ПК
# ============================================================

# Шляхи через змінні середовища (%APPDATA%, %LOCALAPPDATA%), а не з іменем
# користувача: так працює на будь-якому компʼютері і не світить імʼя в репо.
_P = os.path.expandvars
CLAUDE_CMD      = _P(r"%APPDATA%\npm\claude.cmd")
CLAUDE_SESSIONS = os.path.join(os.path.expanduser("~"), ".claude", "projects")

APP_MAP = {
    # Браузери
    "браузер":      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "хром":         r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "chrome":       r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge":         "msedge",
    "microsoft edge": "msedge",

    # Месенджери
    "spotify":      _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "спотіфай":     _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "спотифай":     _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "музика":       _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "discord":      _P(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"),
    "дискорд":      _P(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"),
    "telegram":     _P(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
    "телеграм":     _P(r"%APPDATA%\Telegram Desktop\Telegram.exe"),

    # Microsoft Office
    "ворд":         r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "word":         r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":        r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "ексель":       r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint":   r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    "презентація":  r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",

    # Розробка
    "код":          _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "vscode":       _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "visual studio": _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "android studio": r"C:\Program Files\Android\Android Studio\bin\studio64.exe",
    "xampp":        r"C:\xampp\xampp-control.exe",

    # 3D / Creative
    "blender":      r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
    "unity":        r"C:\Program Files\Unity Hub\Unity Hub.exe",
    "audacity":     r"C:\Program Files\Audacity\Audacity.exe",

    # Ігри
    "steam":        r"C:\Program Files (x86)\Steam\Steam.exe",
    "dota":         r"C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\bin\win64\dota2.exe",
    "elden ring":   r"C:\Program Files (x86)\Steam\steamapps\common\ELDEN RING\Game\eldenring.exe",
    "wallpaper":    r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper32.exe",

    # Системні
    "блокнот":      "notepad",
    "notepad":      "notepad",
    "нотатник":     "notepad",
    "текстовий":    "notepad",
    "нотатки":      f'notepad "{NOTES_PATH}"',
    "мої нотатки":  f'notepad "{NOTES_PATH}"',
    "плани":        f'notepad "{NOTES_PATH}"',
    "калькулятор":  "calc",
    "calculator":   "calc",
    "провідник":    "explorer",
    "файли":        "explorer",
    "paint":        "mspaint",
    "малювання":    "mspaint",
    "7zip":         r"C:\Program Files\7-Zip\7zFM.exe",
    "архіватор":    r"C:\Program Files\7-Zip\7zFM.exe",
    "razer":        r"C:\Program Files (x86)\Razer\Razer Cortex\RazerCortex.exe",

    # AI / Веб-сервіси
    "claude":       "https://claude.ai/new",
    "клод":         "https://claude.ai/new",
    "claude ai":    "https://claude.ai/new",
    "claude code":  f'start cmd /k "{CLAUDE_CMD}"',
    "клод код":     f'start cmd /k "{CLAUDE_CMD}"',
    "chatgpt":      "https://chatgpt.com",
    "чатгпт":       "https://chatgpt.com",
    "gemini":       "https://gemini.google.com",
    "джемін":       "https://gemini.google.com",
    "github":       "https://github.com",
    "гітхаб":       "https://github.com",
}


def open_application(name: str):
    n = name.lower().strip()

    # 1. Точний пошук: ключ цілим словом, довші ключі першими. Раніше ключі
    # перевірялись підрядком у порядку словника, і «клод код» відкривав
    # VS Code (ключ «код»), а «claude code» сайт (ключ «claude»).
    key = vr.match_app_key(n, APP_MAP)
    if key:
        _launch(key, APP_MAP[key])
        return

    # 2. Fuzzy matching — ловить "спотіфай", "блендер", "дискорд" тощо
    best = difflib.get_close_matches(n, APP_MAP.keys(), n=1, cutoff=0.55)
    if best:
        key = best[0]
        log.info(f"Fuzzy match: '{n}' → '{key}'")
        _launch(key, APP_MAP[key])
        return

    # 3. Пошук по словах запиту
    words = n.split()
    for word in words:
        matches = difflib.get_close_matches(word, APP_MAP.keys(), n=1, cutoff=0.7)
        if matches:
            key = matches[0]
            log.info(f"Word fuzzy: '{word}' → '{key}'")
            _launch(key, APP_MAP[key])
            return

    log.warning(f"Програму не знайдено: {name}")
    speak(f"Не знайшла програму «{name}».")
    _mark_action_failed()


def _open_url(url: str):
    """Chrome, якщо він стоїть там, де очікуємо, інакше браузер за замовчуванням."""
    if os.path.exists(CHROME_PATH):
        subprocess.Popen([CHROME_PATH, url])
    else:
        os.startfile(url)


def _launch(key: str, path: str):
    try:
        if path.startswith("http"):
            _open_url(path)
        elif os.path.exists(path):
            # Реальний файл (часто .exe зі ПРОБІЛАМИ у шляху) — через ShellExecute,
            # БЕЗ shell: інакше cmd ламає шлях на пробілі ("C:\Program" + "Files...").
            os.startfile(path)
        else:
            # Прості команди (notepad, calc) і shell-конструкції (start cmd /k "...")
            subprocess.Popen(path, shell=True)
        log.info(f"Відкрито: {key} → {path[:60]}")
    except Exception as e:
        log.warning(f"Помилка відкриття {key}: {e}")


def take_screenshot() -> str | None:
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"lin_{ts}.png")
        pyautogui.screenshot().save(path)
        log.info(f"Скріншот: {path}")
        return path
    except Exception as e:
        log.error(f"Скріншот помилка: {e}")
        return None


def volume_control(direction: str):
    key_map = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}
    steps = 1 if direction == "mute" else 5
    for _ in range(steps):
        pyautogui.hotkey(key_map.get(direction, "volumemute"))
    log.info(f"Гучність: {direction}")


# Системні процеси, які голосом не закриваємо ніколи
_PROTECTED_PROCS = {
    "explorer", "svchost", "csrss", "winlogon", "wininit", "lsass", "services",
    "system", "smss", "dwm", "fontdrvhost", "sihost", "taskhostw", "ctfmon",
    "runtimebroker", "audiodg", "conhost", "searchhost", "startmenuexperiencehost",
}


def kill_process(name: str) -> int:
    """
    Закриває процеси за назвою. Спершу точний збіг («chrome» = chrome.exe),
    якщо такого немає, то за початком назви.

    Раніше збіг шукався підрядком будь-де, тому порожня назва від моделі
    («kill_process:») закрила б УСІ процеси користувача, а «e» половину.
    """
    target = (name or "").lower().strip()
    if target.endswith(".exe"):
        target = target[:-4]
    if len(target) < 3:
        log.warning(f"kill_process: назва '{name}' надто коротка, нічого не чіпаю")
        return 0
    me = os.getpid()
    exact, prefix = [], []
    for proc in psutil.process_iter(["name", "pid"]):
        pname = (proc.info.get("name") or "").lower()
        base = pname[:-4] if pname.endswith(".exe") else pname
        if not base or proc.info.get("pid") == me or base in _PROTECTED_PROCS:
            continue
        if base == target:
            exact.append(proc)
        elif base.startswith(target):
            prefix.append(proc)
    killed = 0
    for proc in exact or prefix:
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    log.info(f"Завершено процесів '{name}': {killed}")
    return killed


CLAUDE_TIMEOUT = 120   # секунд на відповідь Claude Code


def _ask_claude_code(query: str):
    """Відправляє запит в Claude Code через CLI і зачитує відповідь вголос."""
    if not query:
        speak("Що саме запитати у Клода?")
        return

    speak("Запитую Клода, секунду...")

    def _run():
        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", query, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                encoding="utf-8",
                errors="ignore",
            )
            response = (result.stdout or "").strip()
            if not response and result.stderr:
                response = result.stderr.strip()

            if response:
                log.info(f"Claude відповів ({len(response)} символів): {response[:120]}")
                # Для TTS — скорочуємо до ~400 символів, решту зберігаємо в лог
                spoken = response if len(response) <= 400 else response[:400] + "… далі дивись в логах."
                speak(spoken)
            else:
                speak("Клод нічого не відповів.")

        except subprocess.TimeoutExpired:
            # Раніше тут звучало «я ще чекаю, озвучу коли відповість», але
            # subprocess.run уже вбив процес, і відповіді не було б ніколи
            speak(f"Клод не встиг відповісти за {CLAUDE_TIMEOUT // 60} хвилини, я зупинила запит.")
        except FileNotFoundError:
            speak("Клод Код не знайдено. Перевір встановлення.")
            log.error(f"claude.cmd не знайдено: {CLAUDE_CMD}")
        except Exception as e:
            log.error(f"Claude Code помилка: {e}", exc_info=True)
            speak("Щось пішло не так з Клодом.")

    threading.Thread(target=_run, daemon=True).start()


def _notepad_write(text: str):
    """Відкриває Блокнот і вписує текст."""
    if not text:
        speak("Що саме написати в Блокноті?")
        return

    def _run():
        try:
            import pygetwindow as gw

            # Перевіряємо чи Блокнот вже відкритий
            wins = [w for w in gw.getAllWindows()
                    if "notepad" in w.title.lower() or "блокнот" in w.title.lower()
                    or "безіменний" in w.title.lower() or "untitled" in w.title.lower()]

            if not wins:
                subprocess.Popen("notepad.exe")
                time.sleep(1.2)   # чекаємо завантаження
                wins = [w for w in gw.getAllWindows()
                        if "notepad" in w.title.lower() or "безіменний" in w.title.lower()
                        or "untitled" in w.title.lower()]

            if wins:
                w = wins[0]
                w.restore()
                w.activate()
                time.sleep(0.3)
            else:
                log.warning("Блокнот не знайдено після запуску")

            # Вписуємо текст через буфер обміну (підтримує кирилицю)
            import pyperclip
            _saved_clip = pyperclip.paste()   # зберігаємо поточний буфер
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyperclip.copy(_saved_clip)        # відновлюємо
            log.info(f"Блокнот: написано '{text[:60]}'")
            speak("Написала.")
        except Exception as e:
            log.error(f"notepad_write помилка: {e}", exc_info=True)
            speak("Не вдалося написати в Блокноті.")

    threading.Thread(target=_run, daemon=True).start()


def _silent_web_search(query: str):
    """Тихий пошук без відкриття браузера. Результат зачитується вголос."""
    speak("Шукаю...")

    def _run():
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            results = DDGS().text(query, max_results=4)

            if not results:
                speak("Нічого не знайшла по цьому запиту.")
                return

            # Збираємо сніпети
            snippets = []
            for r in results:
                body = r.get("body", "").strip()
                title = r.get("title", "").strip()
                if body:
                    snippets.append(f"{title}: {body[:300]}")

            combined = "\n".join(snippets[:3])
            log.info(f"Web search '{query}': {len(snippets)} результатів")

            # Модель підсумовує коротко для TTS
            resp = llm_chat(
                GROQ_PRIMARY_MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        f"На основі цих результатів пошуку дай КОРОТКУ відповідь "
                        f"(2-3 речення) українською на питання: «{query}»\n\n{combined}"
                    )
                }],
                max_tokens=180,
                temperature=0.4,
            )
            answer = resp.choices[0].message.content.strip()
            log.info(f"Web answer: {answer[:120]}")
            speak(answer)

        except Exception as e:
            log.error(f"Silent search error: {e}", exc_info=True)
            speak("Не вдалося знайти інформацію.")

    threading.Thread(target=_run, daemon=True).start()


def _extract_messages_from_jsonl(path: str) -> list:
    """Витягує (role, text) пари з .jsonl файлу Claude."""
    messages = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    role = obj.get("type") or obj.get("role", "")
                    if role in ("assistant", "human", "user"):
                        content = obj.get("message", {})
                        if isinstance(content, dict):
                            text = ""
                            for block in content.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text += block.get("text", "")
                                elif isinstance(block, str):
                                    text += block
                        elif isinstance(content, str):
                            text = content
                        else:
                            text = str(content)
                        if text.strip():
                            messages.append((role, text.strip()))
                except Exception:
                    pass
    except Exception:
        pass
    return messages


def _list_claude_sessions() -> str:
    """Повертає список проектів / сесій Claude Code."""
    try:
        if not os.path.exists(CLAUDE_SESSIONS):
            return "Папка сесій Claude Code не знайдена."
        projects = []
        for entry in os.scandir(CLAUDE_SESSIONS):
            if entry.is_dir():
                # Знаходимо найновіший .jsonl у проекті
                jsonl = sorted(
                    [f.path for f in os.scandir(entry.path) if f.name.endswith(".jsonl")],
                    key=os.path.getmtime, reverse=True
                )
                if jsonl:
                    msgs = _extract_messages_from_jsonl(jsonl[0])
                    # Перший human-запит = "назва" розмови
                    first = next((t[:60] for r, t in msgs if r in ("human", "user")), entry.name[:40])
                    mtime = datetime.fromtimestamp(os.path.getmtime(jsonl[0]))
                    projects.append((mtime, first))

        if not projects:
            return "Проектів Claude Code не знайдено."

        projects.sort(reverse=True)
        lines = [f"{i+1}. {p[1]} ({p[0].strftime('%d.%m %H:%M')})"
                 for i, p in enumerate(projects[:5])]
        return "Знайдені сесії: " + "; ".join(lines)

    except Exception as e:
        log.error(f"Claude list error: {e}")
        return "Не вдалося отримати список сесій."


def _read_claude_session(n_messages: int = 5, keyword: str = "") -> str:
    """
    Читає повідомлення з Claude Code сесії.
    keyword — якщо задано, шукає файл де зустрічається це слово (пошук за темою).
    Без keyword — бере найновіший файл.
    """
    try:
        # Збираємо всі .jsonl рекурсивно
        jsonl_files = []
        for root_dir, dirs, files in os.walk(CLAUDE_SESSIONS):
            for f in files:
                if f.endswith(".jsonl"):
                    full = os.path.join(root_dir, f)
                    jsonl_files.append((os.path.getmtime(full), full))

        if not jsonl_files:
            return "Сесій Claude Code не знайдено."

        if keyword:
            kw = keyword.lower()
            # Шукаємо файл де текст містить ключове слово
            matches = []
            for mtime, path in sorted(jsonl_files, reverse=True):
                msgs = _extract_messages_from_jsonl(path)
                combined = " ".join(t.lower() for _, t in msgs)
                if kw in combined:
                    score = combined.count(kw)
                    matches.append((score, mtime, path, msgs))
            if not matches:
                return f"Чатів з темою «{keyword}» не знайдено серед {len(jsonl_files)} сесій."
            # Найрелевантніший
            matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
            messages = matches[0][3]
            log.info(f"Claude read: знайдено за '{keyword}' у {matches[0][2]}")
        else:
            _, latest = max(jsonl_files)
            messages = _extract_messages_from_jsonl(latest)

        if not messages:
            return "Повідомлень у сесії не знайдено."

        last = messages[-n_messages:]
        result_lines = []
        for role, text in last:
            label = "Ти" if role in ("human", "user") else "Клод"
            short = text[:200] + ("…" if len(text) > 200 else "")
            result_lines.append(f"{label}: {short}")
        return " / ".join(result_lines)

    except Exception as e:
        log.error(f"Claude session read error: {e}")
        return "Не вдалося прочитати сесію Claude."


def _ask_claude_web(query: str):
    """Відкриває Claude.ai і вводить запит після завантаження сторінки."""
    _open_url("https://claude.ai/new")
    log.info(f"Claude відкрито, запит: '{query}'")
    if not query:
        return

    def _type_after_load():
        time.sleep(4)  # чекаємо завантаження сторінки
        try:
            pyautogui.press("escape")
            time.sleep(0.3)
            # Вводимо через буфер — підтримує кирилицю
            import pyperclip as _pc
            _old = _pc.paste()
            _pc.copy(query)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            _pc.copy(_old)
            pyautogui.press("enter")
            log.info("Claude: запит введено і відправлено")
        except Exception as e:
            log.error(f"Claude type error: {e}")

    threading.Thread(target=_type_after_load, daemon=True).start()


_SPOTIPY_CLIENT = None   # кешований клієнт — не створюємо заново кожного разу
_spotify_lock = threading.Lock()   # серіалізує доступ до токена (без гонок)


_spotify_problem = ""   # чому Spotify API зараз недоступний; це й кажемо голосом


def _spotify_unavailable_msg() -> str:
    return _spotify_problem or "Spotify API не налаштований."


def _get_spotipy():
    """
    Повертає авторизований Spotipy клієнт або None.

    Ніколи не доходить до інтерактивного входу: без придатного токена spotipy
    сам викликає input(), а під pythonw консолі немає («lost sys.stdin»).
    Тому токен перевіряємо тут: чи є він, чи має всі дозволи (spotify_common),
    і оновлюємо проактивно під замком. Причину відмови кладемо в
    _spotify_problem, щоб сказати голосом, що саме зробити.
    """
    global _SPOTIPY_CLIENT, _spotify_problem
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        _spotify_problem = "Spotify не налаштований: додай ключі в secrets.json."
        return None
    with _spotify_lock:
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
            from spotipy.cache_handler import CacheFileHandler

            if _SPOTIPY_CLIENT is None:
                auth = SpotifyOAuth(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET,
                    redirect_uri=spotify_common.REDIRECT_URI,
                    scope=spotify_common.SCOPES,
                    cache_handler=CacheFileHandler(cache_path=spotify_common.TOKEN_PATH),
                    open_browser=False,   # під pythonw браузер/stdin недоступні
                )
                _SPOTIPY_CLIENT = spotipy.Spotify(auth_manager=auth)
                log.info("Spotipy клієнт ініціалізовано")

            am = _SPOTIPY_CLIENT.auth_manager
            tok = am.cache_handler.get_cached_token()
            if not tok:
                _spotify_problem = "Spotify ще не авторизований. Запусти spotify_auth.bat."
                log.error("Spotify: токена немає, потрібен spotify_auth.bat")
                return None
            if not spotify_common.token_has_scopes(tok):
                _spotify_problem = "Токену Spotify бракує дозволів. Запусти spotify_auth.bat ще раз."
                log.error(f"Spotify: у токені не всі дозволи ({tok.get('scope')})")
                return None
            if am.is_token_expired(tok):
                try:
                    am.refresh_access_token(tok["refresh_token"])
                    log.info("Spotify токен оновлено")
                except Exception as e:
                    _spotify_problem = "Не вдалося оновити токен Spotify. Запусти spotify_auth.bat."
                    log.error(f"Spotify refresh: {e}")
                    return None
            _spotify_problem = ""
            return _SPOTIPY_CLIENT
        except Exception as e:
            _spotify_problem = "Spotify зараз не відповідає."
            log.error(f"Spotipy init/refresh: {e}")
            return None


def _spotify_transfer_device(hint: str):
    """Перемикає відтворення Spotify на пристрій за підказкою (телефон/пк/назва)."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            speak("Spotify не знайшов активних пристроїв. Відкрий Spotify на потрібному пристрої.")
            return

        h = hint.lower()
        # Мапи підказок до типів пристроїв
        phone_hints   = {"телефон", "phone", "mobile", "мобільний", "смартфон"}
        pc_hints      = {"пк", "pc", "комп", "комп'ютер", "computer", "ноут", "laptop", "windows"}

        target = None
        for d in devices:
            name = d["name"].lower()
            dtype = d.get("type", "").lower()
            if h in phone_hints and dtype in ("smartphone",):
                target = d; break
            if h in pc_hints and dtype in ("computer",):
                target = d; break
            if h in name:
                target = d; break

        if not target:
            # Fuzzy match по імені пристрою
            names = [d["name"] for d in devices]
            close = difflib.get_close_matches(hint, [n.lower() for n in names], n=1, cutoff=0.4)
            if close:
                target = next(d for d in devices if d["name"].lower() == close[0])

        if not target:
            names_str = ", ".join([f"{d['name']} ({d['type']})" for d in devices])
            speak(f"Знайдені пристрої: {names_str}. Скажи точніше.")
            return

        sp.transfer_playback(target["id"], force_play=True)
        speak(f"Перемикаю відтворення на {target['name']}.")
        log.info(f"Spotify device: {target['name']}")
    except Exception as e:
        log.error(f"Spotify device помилка: {e}")
        speak("Не вдалося перемкнути пристрій.")


def _spotify_set_playing(want_playing: bool):
    """
    Пауза або продовження САМЕ як сказано. Раніше і «пауза», і «грай» були
    перемикачем: «постав на паузу», коли вже на паузі, вмикало музику.
    Без API лишається тільки медіа-клавіша, а вона вміє лише перемикати.
    """
    sp = _get_spotipy()
    if sp:
        try:
            state = sp.current_playback()
            playing = bool(state and state.get("is_playing"))
            if playing == want_playing:
                log.info(f"Spotify: вже {'грає' if playing else 'на паузі'}, нічого не міняю")
                return
            if want_playing:
                sp.start_playback()
            else:
                sp.pause_playback()
            log.info(f"Spotify API: {'resumed' if want_playing else 'paused'}")
            return
        except Exception as e:
            log.warning(f"Spotify pause/resume API fail: {e}")
    pyautogui.hotkey("playpause")
    log.info("Spotify: media key playpause")


def _spotify_now_playing():
    """Вголос каже що зараз грає."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        state = sp.current_playback()
        if not state or not state.get("item"):
            speak("Зараз нічого не грає.")
            return
        item   = state["item"]
        track  = item["name"]
        artist = ", ".join(a["name"] for a in item["artists"])
        album  = item["album"]["name"]
        is_playing = state.get("is_playing", False)
        status = "Грає" if is_playing else "На паузі"
        speak(f"{status}: {track} — {artist}, альбом {album}.")
        log.info(f"Spotify now: {track} / {artist}")
    except Exception as e:
        log.error(f"Spotify now playing: {e}")
        speak("Не вдалося отримати інфо про трек.")


def _spotify_recent(limit: int = 5):
    """Вголос каже нещодавно прослухані треки."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_recently_played(limit=limit)
        items = results.get("items", [])
        if not items:
            speak("Нещодавно прослуханих треків не знайдено.")
            return
        seen = []
        lines = []
        for it in items:
            track  = it["track"]["name"]
            artist = it["track"]["artists"][0]["name"]
            key    = f"{track}|{artist}"
            if key not in seen:
                seen.append(key)
                lines.append(f"{track} від {artist}")
            if len(lines) >= 3:
                break
        speak("Нещодавно слухав: " + "; ".join(lines) + ".")
        log.info(f"Spotify recent: {lines}")
    except Exception as e:
        log.error(f"Spotify recent: {e}")
        speak("Не вдалося отримати список.")


def _spotify_liked(limit: int = 5):
    """Вголос каже кілька лайкнутих треків."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_saved_tracks(limit=limit)
        items = results.get("items", [])
        if not items:
            speak("Лайкнутих треків не знайдено.")
            return
        lines = []
        for it in items:
            track  = it["track"]["name"]
            artist = it["track"]["artists"][0]["name"]
            lines.append(f"{track} від {artist}")
        speak("Твої лайкнуті: " + "; ".join(lines) + ".")
        log.info(f"Spotify liked: {lines}")
    except Exception as e:
        log.error(f"Spotify liked: {e}")
        speak("Не вдалося отримати лайкнуті треки.")


def _get_weather(city: str = None) -> str:
    """Отримує погоду через wttr.in (без API ключа)."""
    city = city or WEATHER_CITY
    try:
        import urllib.request, urllib.parse
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=uk"
        req = urllib.request.Request(url, headers={"User-Agent": "Lin/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())

        cur = data["current_condition"][0]
        temp     = cur["temp_C"]
        feels    = cur["FeelsLikeC"]
        humidity = cur["humidity"]
        wind     = cur["windspeedKmph"]

        # Опис — спробуємо Ukrainian, інакше English
        lang_list = cur.get("lang_uk") or cur.get("weatherDesc", [])
        desc = lang_list[0]["value"] if lang_list else "невідомо"

        # Завтра
        tomorrow  = data["weather"][1]
        t_max     = tomorrow["maxtempC"]
        t_min     = tomorrow["mintempC"]
        hourly    = tomorrow.get("hourly", [])
        rain_chance = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)

        rain_note = f" Завтра ймовірність дощу {rain_chance}%." if rain_chance >= 40 else ""
        result = (
            f"{desc}, {temp}°, відчувається як {feels}°. "
            f"Вологість {humidity}%, вітер {wind} км/г. "
            f"Завтра від {t_min} до {t_max}°.{rain_note}"
        )
        log.info(f"Weather: {result}")
        return result

    except Exception as e:
        log.error(f"Weather error: {e}")
        return "Не вдалося отримати погоду. Перевір інтернет."


def _get_system_info() -> str:
    """Повертає стан CPU, RAM, диску і батареї."""
    parts = []

    # CPU (0.5 сек вимірювання)
    cpu = psutil.cpu_percent(interval=0.5)
    parts.append(f"CPU {cpu:.0f}%")

    # RAM
    mem = psutil.virtual_memory()
    used_gb  = mem.used  / 1024 ** 3
    total_gb = mem.total / 1024 ** 3
    parts.append(f"пам'ять {used_gb:.1f} з {total_gb:.1f} ГБ")

    # Диск C:
    try:
        disk = psutil.disk_usage("C:\\")
        free_gb = disk.free / 1024 ** 3
        parts.append(f"диск C: {free_gb:.0f} ГБ вільно")
    except Exception:
        pass

    # Батарея (якщо є)
    batt = psutil.sensors_battery()
    if batt is not None:
        status = "заряджається" if batt.power_plugged else "від батареї"
        parts.append(f"батарея {batt.percent:.0f}% ({status})")

    result = "Система: " + ", ".join(parts) + "."
    log.info(f"System info: {result}")
    return result


# ============================================================
#  ТАЙМЕР
# ============================================================
_active_timers: dict[str, bool] = {}   # id → активний
_timer_seq = itertools.count(1)

def _timer_start(seconds: int, label: str = ""):
    """Запускає зворотний відлік. По закінченні — звук + голос."""
    if seconds <= 0:
        speak("Невірний час для таймера.")
        return

    # Лічильник, а не секунди: два таймери, запущені в ту саму секунду, мали
    # один id, і другий по завершенні мовчки зникав
    timer_id = f"timer_{next(_timer_seq)}"
    _active_timers[timer_id] = True

    mins, secs = divmod(seconds, 60)
    if mins and secs:
        time_str = f"{mins} хв {secs} сек"
    elif mins:
        time_str = vr.count(mins, "хвилина", "хвилини", "хвилин")
    else:
        time_str = vr.count(secs, "секунда", "секунди", "секунд")

    name = f"«{label}» — " if label else ""
    log.info(f"Таймер {timer_id}: {time_str}")

    def _run():
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            if not _active_timers.get(timer_id):
                log.info(f"Таймер {timer_id} скасовано")
                return
            time.sleep(0.5)
        if not _active_timers.get(timer_id):
            return
        _active_timers.pop(timer_id, None)
        # Звуковий сигнал через winsound (вбудовано в Windows)
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(880, 300)
                time.sleep(0.15)
        except Exception:
            pass
        speak(f"{name}Таймер {time_str} — час!")
        log.info(f"Таймер {timer_id} завершено")

    threading.Thread(target=_run, daemon=True).start()
    return f"Таймер {time_str} запущено."


def _timer_stop_all():
    """Скасовує всі активні таймери."""
    count = sum(1 for v in _active_timers.values() if v)
    for k in list(_active_timers):
        _active_timers[k] = False
    if count:
        return f"Скасовано {vr.count(count, 'таймер', 'таймери', 'таймерів')}."
    return "Активних таймерів немає."


# ============================================================
#  БУФЕР ОБМІНУ
# ============================================================

def _clipboard_read() -> str:
    """Зачитує вголос вміст буфера обміну."""
    try:
        import pyperclip
        text = pyperclip.paste().strip()
        if not text:
            return "Буфер порожній."
        short = text[:300] + ("…" if len(text) > 300 else "")
        return f"В буфері: {short}"
    except Exception as e:
        log.error(f"Clipboard read: {e}")
        return "Не вдалося прочитати буфер."


def _clipboard_save() -> str:
    """Зберігає вміст буфера як нотатку."""
    try:
        import pyperclip
        text = pyperclip.paste().strip()
        if not text:
            return "Буфер порожній — нічого зберігати."
        short = text[:200]
        return note_add(short)
    except Exception as e:
        log.error(f"Clipboard save: {e}")
        return "Не вдалося зберегти буфер."


def _clipboard_set(text: str) -> str:
    """Копіює текст в буфер обміну."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return f"Скопіювала: «{text[:60]}»."
    except Exception as e:
        log.error(f"Clipboard set: {e}")
        return "Не вдалося скопіювати."


# ============================================================
#  ШВИДКІСТЬ ГОЛОСУ
# ============================================================

def _adjust_voice_rate(direction: str) -> str:
    """Змінює швидкість TTS. direction: faster / slower / reset"""
    global VOICE_RATE, _VOICE_RATE_VALUE
    if direction == "faster":
        _VOICE_RATE_VALUE = min(_VOICE_RATE_VALUE + _VOICE_RATE_STEP, _VOICE_RATE_MAX)
    elif direction == "slower":
        _VOICE_RATE_VALUE = max(_VOICE_RATE_VALUE - _VOICE_RATE_STEP, _VOICE_RATE_MIN)
    else:
        _VOICE_RATE_VALUE = 25
    sign = "+" if _VOICE_RATE_VALUE >= 0 else ""
    VOICE_RATE = f"{sign}{_VOICE_RATE_VALUE}%"
    log.info(f"Voice rate: {VOICE_RATE}")
    if direction == "reset":
        return "Швидкість повернута до нормальної."
    return f"{'Швидше' if direction == 'faster' else 'Повільніше'}. Зараз {VOICE_RATE}."


# ============================================================
#  КОНВЕРТЕР ВАЛЮТ
# ============================================================

def _currency_convert(amount: float, from_cur: str, to_cur: str) -> str:
    """Конвертує валюти через open.er-api.com (без ключа, 170+ валют включно з UAH)."""
    try:
        import urllib.request
        from_c = from_cur.upper().strip()
        to_c   = to_cur.upper().strip()
        url = f"https://open.er-api.com/v6/latest/{from_c}"
        req = urllib.request.Request(url, headers={"User-Agent": "Lin/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        if data.get("result") != "success":
            return f"Не вдалося отримати курс {from_c}."
        rate = data["rates"].get(to_c)
        if rate is None:
            return f"Валюта {to_c} не знайдена."
        result_val = round(amount * rate, 2)
        names = {
            "USD": "доларів", "EUR": "євро", "UAH": "гривень",
            "PLN": "злотих",  "GBP": "фунтів", "JPY": "єн",
            "CHF": "франків", "CZK": "крон",    "SEK": "крон",
            "NOK": "крон",    "CAD": "канадських доларів",
            "AUD": "австралійських доларів",     "BYN": "білоруських рублів",
            "RUB": "рублів",  "TRY": "лір",      "CNY": "юанів",
        }
        from_n = names.get(from_c, from_c)
        to_n   = names.get(to_c,   to_c)
        return f"{amount:g} {from_n} = {result_val:g} {to_n}."
    except Exception as e:
        log.error(f"Currency convert: {e}")
        return "Не вдалося конвертувати. Перевір інтернет."


def _spotify_pick_device(sp):
    """
    Знаходить пристрій Spotify для керування гучністю.
    Повертає (device_id, volume_percent) або (None, None).
    Спочатку активний пристрій, потім будь-який доступний (краще комп'ютер).
    """
    # 1. Активний пристрій з current_playback
    try:
        state = sp.current_playback()
        if state and state.get("device") and state["device"].get("id"):
            d = state["device"]
            return d["id"], d.get("volume_percent")
    except Exception as e:
        log.debug(f"_spotify_pick_device current_playback: {e}")

    # 2. Будь-який пристрій зі списку (desktop app буде тут навіть якщо не «активний»)
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            return None, None
        active = next((d for d in devices if d.get("is_active")), None)
        computer = next((d for d in devices if d.get("type") == "Computer"), None)
        chosen = active or computer or devices[0]
        return chosen["id"], chosen.get("volume_percent")
    except Exception as e:
        log.debug(f"_spotify_pick_device devices: {e}")
        return None, None


def _spotify_volume(direction: str, value: int = 10) -> None:
    """
    Змінює гучність САМЕ Spotify через API (ніколи не чіпає системну гучність).
    direction: "up" | "down" | "set"
    """
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return

    device_id, current_vol = _spotify_pick_device(sp)
    if device_id is None:
        speak("Spotify не запущений на жодному пристрої. Відкрий застосунок і увімкни щось.")
        return
    if current_vol is None:
        current_vol = 50

    if direction == "up":
        new_vol = min(current_vol + value, 100)
    elif direction == "down":
        new_vol = max(current_vol - value, 0)
    else:  # "set"
        new_vol = max(0, min(value, 100))

    try:
        sp.volume(new_vol, device_id=device_id)
        log.info(f"Spotify volume: {current_vol}% → {new_vol}% (device {device_id[:8]})")
        speak(f"Гучність Spotify {new_vol} відсотків.")
    except Exception as e:
        es = str(e)
        log.warning(f"Spotify volume API fail: {es}")
        if "403" in es or "premium" in es.lower():
            speak("Керування гучністю Spotify працює тільки з Premium підпискою.")
        elif "VOLUME" in es.upper() or "control device volume" in es.lower():
            speak("Цей пристрій не дозволяє керувати гучністю. Спробуй на телефоні.")
        else:
            speak("Не вдалося змінити гучність Spotify.")


def _maybe_spotify_volume(direction: str) -> bool:
    """
    Для НЕЯВНОЇ команди гучності ('гучніше' без слова spotify):
    керуємо Spotify лише якщо він зараз АКТИВНЕ джерело звуку
    (грає або має активний пристрій). Інакше False → системна гучність.
    """
    sp = _get_spotipy()
    if not sp:
        return False
    try:
        # Грає прямо зараз?
        state = sp.current_playback()
        if state and state.get("is_playing"):
            _spotify_volume(direction)
            return True
        # Або є активний пристрій (на паузі, але обраний у Connect)?
        devices = sp.devices().get("devices", [])
        if any(d.get("is_active") for d in devices):
            _spotify_volume(direction)
            return True
    except Exception as e:
        log.debug(f"_maybe_spotify_volume: {e}")
    return False


def _spotify_shuffle(enable: bool) -> None:
    """Вмикає або вимикає shuffle через API."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        sp.shuffle(enable)
        state_str = "увімкнено" if enable else "вимкнено"
        log.info(f"Spotify shuffle: {state_str}")
        speak(f"Перемішування {state_str}.")
    except Exception as e:
        log.error(f"Spotify shuffle: {e}")
        speak("Не вдалося змінити режим перемішування.")


def _spotify_repeat(mode: str) -> None:
    """
    Встановлює режим повтору через API.
    mode: "track" | "context" | "off"
    """
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    mode_map = {
        "track": "track", "трек": "track", "пісня": "track", "один": "track",
        "context": "context", "плейлист": "context", "альбом": "context", "все": "context",
        "off": "off", "вимкнути": "off", "ні": "off", "стоп": "off",
    }
    api_mode = mode_map.get(mode.lower(), "off")
    try:
        sp.repeat(api_mode)
        labels = {"track": "повтор треку", "context": "повтор плейлисту", "off": "повтор вимкнено"}
        log.info(f"Spotify repeat: {api_mode}")
        speak(f"{labels[api_mode]}.")
    except Exception as e:
        log.error(f"Spotify repeat: {e}")
        speak("Не вдалося змінити режим повтору.")


def _spotify_playlists(limit: int = 10) -> None:
    """Вголос перераховує плейлисти користувача."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_playlists(limit=limit)
        items = results.get("items", [])
        if not items:
            speak("Плейлистів не знайдено.")
            return
        names = [it["name"] for it in items if it]
        speak("Твої плейлисти: " + ", ".join(names) + ".")
        log.info(f"Spotify playlists: {names}")
    except Exception as e:
        log.error(f"Spotify playlists: {e}")
        speak("Не вдалося отримати плейлисти.")


def _spotify_play_playlist(name: str) -> None:
    """Шукає плейлист за назвою і відтворює його."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        # Спочатку шукаємо серед власних плейлистів
        results = sp.current_user_playlists(limit=50)
        items = [it for it in results.get("items", []) if it]
        names = [it["name"] for it in items]

        # Fuzzy пошук
        close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
        if close:
            playlist = next(it for it in items if it["name"] == close[0])
        else:
            # Якщо не знайшли серед своїх — глобальний пошук
            sr_res = sp.search(q=name, type="playlist", limit=1)
            pl_items = sr_res.get("playlists", {}).get("items", [])
            if not pl_items:
                speak(f"Плейлист «{name}» не знайдено.")
                return
            playlist = pl_items[0]

        sp.start_playback(context_uri=playlist["uri"])
        log.info(f"Spotify play playlist: {playlist['name']}")
        speak(f"Вмикаю плейлист «{playlist['name']}».")
    except Exception as e:
        log.error(f"Spotify play playlist: {e}")
        if "No active device" in str(e):
            speak("Відкрий Spotify на телефоні або ПК, щоб я могла відтворити.")
        else:
            speak("Не вдалося відтворити плейлист.")


def _spotify_queue(limit: int = 5) -> None:
    """Показує наступні треки в черзі."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        queue_data = sp.queue()
        queue = queue_data.get("queue", [])
        if not queue:
            speak("Черга порожня.")
            return
        lines = []
        for t in queue[:limit]:
            track  = t["name"]
            artist = t["artists"][0]["name"]
            lines.append(f"{track} від {artist}")
        speak("Далі в черзі: " + "; ".join(lines) + ".")
        log.info(f"Spotify queue: {lines}")
    except Exception as e:
        log.error(f"Spotify queue: {e}")
        speak("Не вдалося отримати чергу.")


def _spotify_playlist_tracks(name: str) -> None:
    """
    Зачитує перші 10 треків з плейлисту.
    Якщо name порожній — з поточного контексту відтворення.
    """
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        if not name:
            # Беремо поточний контекст
            state = sp.current_playback()
            if not state or not state.get("context"):
                speak("Зараз не грає жоден плейлист.")
                return
            ctx = state["context"]
            if ctx["type"] != "playlist":
                speak("Зараз грає не плейлист.")
                return
            pl_id = ctx["uri"].split(":")[-1]
            pl_info = sp.playlist(pl_id, fields="name,tracks.items(track(name,artists))")
            pl_name = pl_info["name"]
            tracks_raw = pl_info["tracks"]["items"]
        else:
            # Шукаємо плейлист за назвою
            results = sp.current_user_playlists(limit=50)
            items = [it for it in results.get("items", []) if it]
            names = [it["name"] for it in items]
            close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
            if not close:
                speak(f"Плейлист «{name}» не знайдено.")
                return
            playlist = next(it for it in items if it["name"] == close[0])
            pl_name = playlist["name"]
            pl_info = sp.playlist(playlist["id"], fields="tracks.items(track(name,artists))")
            tracks_raw = pl_info["tracks"]["items"]

        lines = []
        for it in tracks_raw[:10]:
            t = it.get("track")
            if t:
                lines.append(f"{t['name']} — {t['artists'][0]['name']}")

        if not lines:
            speak(f"В плейлисті «{pl_name}» немає треків.")
            return

        speak(f"Плейлист «{pl_name}»: " + "; ".join(lines) + ".")
        log.info(f"Playlist tracks '{pl_name}': {len(lines)} треків")
    except Exception as e:
        log.error(f"Spotify playlist tracks: {e}")
        speak("Не вдалося отримати треки плейлисту.")


def _spotify_add_to_queue(query: str) -> None:
    """Додає трек в чергу Spotify за назвою."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.search(q=query, type="track", limit=1)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            speak(f"Трек «{query}» не знайдено.")
            return
        track = tracks[0]
        sp.add_to_queue(track["uri"])
        name = track["name"]
        artist = track["artists"][0]["name"]
        log.info(f"Spotify queue add: {name} / {artist}")
        speak(f"Додала «{name}» від {artist} в чергу.")
    except Exception as e:
        log.error(f"Spotify add to queue: {e}")
        if "No active device" in str(e):
            speak("Відкрий Spotify на пристрої, щоб додати в чергу.")
        else:
            speak("Не вдалося додати в чергу.")


def _spotify_list_devices():
    """Голосом повідомляє список активних пристроїв Spotify."""
    sp = _get_spotipy()
    if not sp:
        speak(_spotify_unavailable_msg())
        return
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            speak("Активних пристроїв Spotify не знайдено.")
            return
        names = [f"{d['name']} ({d['type']})" for d in devices]
        speak("Доступні пристрої Spotify: " + ", ".join(names))
    except Exception as e:
        log.error(f"Spotify list devices: {e}")


# ============================================================
#  SLACK
# ============================================================
_slack_client_cache = None


def _get_slack():
    """Повертає ініціалізований Slack WebClient (з кешу)."""
    global _slack_client_cache
    if _slack_client_cache is not None:
        return _slack_client_cache
    if not SLACK_TOKEN:
        return None
    try:
        from slack_sdk import WebClient
        cl = WebClient(token=SLACK_TOKEN)
        cl.auth_test()   # перевірка — кидає помилку якщо токен невалідний
        _slack_client_cache = cl
        log.info("Slack клієнт ініціалізовано")
        return _slack_client_cache
    except Exception as e:
        log.error(f"Slack init: {e}")
        return None


def _slack_user_name(sl, user_id: str) -> str:
    """Повертає читабельне ім'я Slack-користувача."""
    try:
        info = sl.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("real_name") or profile.get("display_name") or user_id
    except Exception:
        return user_id


def _slack_unread(limit: int = 5) -> None:
    """Читає непрочитані повідомлення через search API."""
    sl = _get_slack()
    if not sl:
        speak("Slack не налаштований. Додай SLACK_TOKEN в конфіг lin.py.")
        return
    try:
        result = sl.search_messages(query="is:unread", count=limit, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            speak("Непрочитаних повідомлень у Slack немає.")
            return
        lines = []
        for m in matches[:limit]:
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            ch    = m.get("channel", {}).get("name", "")
            ch_str = f"в #{ch}" if ch else ""
            lines.append(f"{uname} {ch_str}: {text}")
        speak(f"Непрочитані в Slack ({len(matches)}): " + "; ".join(lines) + ".")
        log.info(f"Slack unread: {len(matches)} повідомлень")
    except Exception as e:
        log.error(f"Slack unread: {e}")
        speak("Не вдалося прочитати Slack. Перевір токен і scope search:read.")


def _slack_channel(channel_name: str, limit: int = 5) -> None:
    """Читає останні повідомлення з каналу."""
    sl = _get_slack()
    if not sl:
        speak("Slack не налаштований.")
        return
    try:
        # Шукаємо канал серед доступних
        resp = sl.conversations_list(types="public_channel,private_channel", limit=200)
        channels = resp.get("channels", [])
        ch = next(
            (c for c in channels if channel_name.lower() in c["name"].lower()),
            None
        )
        if not ch:
            speak(f"Канал «{channel_name}» не знайдено.")
            return
        hist = sl.conversations_history(channel=ch["id"], limit=limit)
        msgs = hist.get("messages", [])
        if not msgs:
            speak(f"В #{ch['name']} порожньо.")
            return
        lines = []
        for m in reversed(msgs[:limit]):
            uid   = m.get("user", "")
            uname = _slack_user_name(sl, uid) if uid else "бот"
            text  = (m.get("text") or "")[:100]
            lines.append(f"{uname}: {text}")
        speak(f"#{ch['name']}: " + "; ".join(lines) + ".")
        log.info(f"Slack channel #{ch['name']}: {len(lines)} повідомлень")
    except Exception as e:
        log.error(f"Slack channel: {e}")
        speak("Не вдалося прочитати канал.")


def _slack_dm(limit: int = 5) -> None:
    """Читає останні приватні повідомлення (DM)."""
    sl = _get_slack()
    if not sl:
        speak("Slack не налаштований.")
        return
    try:
        ims = sl.conversations_list(types="im", limit=20).get("channels", [])
        if not ims:
            speak("Немає активних DM у Slack.")
            return
        all_msgs = []
        for im in ims[:8]:
            hist = sl.conversations_history(channel=im["id"], limit=2).get("messages", [])
            for m in hist:
                text = (m.get("text") or "").strip()
                if not text:
                    continue
                uid   = im.get("user", "")
                uname = _slack_user_name(sl, uid) if uid else "хтось"
                ts    = float(m.get("ts", 0))
                all_msgs.append((ts, uname, text[:80]))
        if not all_msgs:
            speak("Нових DM немає.")
            return
        all_msgs.sort(reverse=True)
        lines = [f"{u}: {t}" for _, u, t in all_msgs[:limit]]
        speak("Приватні повідомлення: " + "; ".join(lines) + ".")
        log.info(f"Slack DM: {len(lines)} повідомлень")
    except Exception as e:
        log.error(f"Slack DM: {e}")
        speak("Не вдалося прочитати DM.")


def _slack_mentions(limit: int = 5) -> None:
    """Читає згадки (@you) через search."""
    sl = _get_slack()
    if not sl:
        speak("Slack не налаштований.")
        return
    try:
        result = sl.search_messages(query="is:mention", count=limit, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            speak("Згадок у Slack немає.")
            return
        lines = []
        for m in matches[:limit]:
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            lines.append(f"{uname}: {text}")
        speak(f"Тебе згадали {vr.count(len(matches), 'раз', 'рази', 'разів')}: " + "; ".join(lines) + ".")
        log.info(f"Slack mentions: {len(matches)}")
    except Exception as e:
        log.error(f"Slack mentions: {e}")
        speak("Не вдалося отримати згадки.")


# ============================================================
#  GMAIL
# ============================================================
_gmail_service_cache = None


_google_creds_cache = None


def _get_google_creds():
    """Спільні OAuth2 креденшели для Gmail і Calendar (один токен, з кешу)."""
    global _google_creds_cache
    if _google_creds_cache is not None:
        return _google_creds_cache
    if not os.path.exists(GMAIL_CREDENTIALS_PATH):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

        creds = None
        if os.path.exists(GMAIL_TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GMAIL_TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        _google_creds_cache = creds
        return creds
    except Exception as e:
        log.error(f"Google creds init: {e}")
        return None


def _get_gmail():
    """Повертає ініціалізований Gmail сервіс (OAuth2, з кешу)."""
    global _gmail_service_cache
    if _gmail_service_cache is not None:
        return _gmail_service_cache
    creds = _get_google_creds()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build
        _gmail_service_cache = build("gmail", "v1", credentials=creds)
        log.info("Gmail сервіс ініціалізовано")
        return _gmail_service_cache
    except Exception as e:
        log.error(f"Gmail init: {e}")
        return None


_calendar_service_cache = None


def _get_calendar():
    """Повертає ініціалізований Google Calendar сервіс (з кешу)."""
    global _calendar_service_cache
    if _calendar_service_cache is not None:
        return _calendar_service_cache
    creds = _get_google_creds()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build
        _calendar_service_cache = build("calendar", "v3", credentials=creds)
        log.info("Calendar сервіс ініціалізовано")
        return _calendar_service_cache
    except Exception as e:
        log.error(f"Calendar init: {e}")
        return None


# ── Мульти-акаунт пошта ───────────────────────────────────────────────────────
_gmail_multi_cache: dict = {}   # token_path → service


def _get_gmail_service(token_path: str):
    """Будує Gmail сервіс для конкретного токена (без інтерактивного входу)."""
    if token_path in _gmail_multi_cache:
        return _gmail_multi_cache[token_path]
    if not os.path.exists(GMAIL_CREDENTIALS_PATH) or not os.path.exists(token_path):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        svc = build("gmail", "v1", credentials=creds)
        _gmail_multi_cache[token_path] = svc
        return svc
    except Exception as e:
        log.error(f"Gmail service {os.path.basename(token_path)}: {e}")
        return None


def _gmail_accounts() -> list:
    """Список (label, service) для всіх авторизованих скриньок."""
    out = []
    for acc in GMAIL_ACCOUNTS:
        svc = _get_gmail_service(acc["token"])
        if svc:
            out.append((acc["label"], svc))
    return out


def _gmail_box(label: str) -> str:
    """Який набір правил сортування застосовувати до цієї скриньки."""
    for acc in GMAIL_ACCOUNTS:
        if acc["label"] == label:
            return acc.get("box", "основна")
    return "основна"


_triage_label_cache: dict = {}   # token_path -> {категорія: labelId}


def _triage_labels(svc, token_path: str) -> dict:
    """{категорія: labelId} для скриньки; мітки створюються при першому зверненні."""
    labels = _triage_label_cache.get(token_path)
    if labels is None:
        labels = mail_triage.ensure_labels(svc)
        _triage_label_cache[token_path] = labels
    return labels


def _gmail_triage(svc, token_path: str, msg_id: str, sender: str, subject: str, box: str):
    """
    Вішає мітку Трекер/* на один лист і прибирає шум із вхідних.
    Повертає категорію, або None якщо сортування вимкнене чи щось пішло не так.

    Помилка тут не має ламати монітор: не змогли повісити мітку, то й добре,
    лист все одно буде оголошений, якщо він того вартий.
    """
    if not (MAIL_TRIAGE_ENABLED and mail_triage):
        return None
    try:
        cat = mail_triage.classify(sender, subject, box)
        labels = _triage_labels(svc, token_path)
        body = {"addLabelIds": [labels[cat]]}
        if cat in mail_triage.ARCHIVE:
            body["removeLabelIds"] = ["INBOX"]
        svc.users().messages().modify(userId="me", id=msg_id, body=body).execute()
        return cat
    except Exception as e:
        log.error(f"Сортування листа {msg_id}: {e}")
        return None


def _gmail_find(query: str):
    """Знаходить перший лист за запитом серед усіх скриньок. → (label, svc, msg_id) або None."""
    q = query.strip() or "is:unread"
    for label, svc in _gmail_accounts():
        try:
            res = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
            msgs = res.get("messages", [])
            if msgs:
                return label, svc, msgs[0]["id"]
        except Exception as e:
            log.debug(f"Gmail find ({label}): {e}")
    return None


def _gmail_parse_sender(raw: str) -> str:
    """'John Doe <john@example.com>' → 'John Doe'"""
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"').strip("'")
    return raw.strip()


def _gmail_headers(msg: dict) -> dict:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _gmail_unread(limit: int = 5) -> None:
    """Читає непрочитані листи з усіх скриньок — скільки і від кого."""
    accounts = _gmail_accounts()
    if not accounts:
        speak("Gmail не налаштований. Потрібен файл gmail_credentials.json в папці асистента.")
        return
    multi = len(accounts) > 1
    parts = []
    grand_total = 0
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(
                userId="me", labelIds=["UNREAD", "INBOX"], maxResults=limit
            ).execute()
            msgs  = res.get("messages", [])
            total = res.get("resultSizeEstimate", len(msgs))
            grand_total += total
            if not msgs:
                parts.append(f"{label}: чисто" if multi else "")
                continue
            senders = []
            for m in msgs[:3]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata", metadataHeaders=["From"]
                ).execute()
                senders.append(_gmail_parse_sender(_gmail_headers(full).get("From", "?")))
            uniq = ", ".join(dict.fromkeys(senders))
            if multi:
                parts.append(f"{label}: {total} від {uniq}")
            else:
                parts.append(f"У тебе {vr.count(total, 'непрочитаний лист', 'непрочитані листи', 'непрочитаних листів')}, від {uniq}")
            log.info(f"Gmail unread [{label}]: {total}")
        except Exception as e:
            log.error(f"Gmail unread [{label}]: {e}")

    parts = [p for p in parts if p]
    if grand_total == 0:
        speak("Непрочитаних листів немає. Усі скриньки чисті." if multi else
              "Непрочитаних листів немає. Поштова скринька чиста.")
        return
    speak(("Пошта. " if multi else "") + ". ".join(parts) + ".")


def _gmail_latest(limit: int = 3) -> None:
    """Читає останні листи з усіх скриньок."""
    accounts = _gmail_accounts()
    if not accounts:
        speak("Gmail не налаштований.")
        return
    multi = len(accounts) > 1
    parts = []
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], maxResults=limit
            ).execute()
            msgs = res.get("messages", [])
            if not msgs:
                continue
            lines = []
            for m in msgs[:limit]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject"]
                ).execute()
                h = _gmail_headers(full)
                sender  = _gmail_parse_sender(h.get("From", "невідомо"))
                subject = h.get("Subject", "без теми")
                lines.append(f"{sender}: «{subject}»")
            prefix = f"{label} — " if multi else ""
            parts.append(prefix + "; ".join(lines))
        except Exception as e:
            log.error(f"Gmail latest [{label}]: {e}")
    if not parts:
        speak("Листів немає.")
        return
    speak("Останні листи: " + ". ".join(parts) + ".")


def _gmail_search(query: str, limit: int = 3) -> None:
    """Шукає листи за запитом серед усіх скриньок (синтаксис Gmail: from:, subject:)."""
    accounts = _gmail_accounts()
    if not accounts:
        speak("Gmail не налаштований.")
        return
    multi = len(accounts) > 1
    parts = []
    found = 0
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(userId="me", q=query, maxResults=limit).execute()
            msgs = res.get("messages", [])
            if not msgs:
                continue
            lines = []
            for m in msgs[:limit]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject"]
                ).execute()
                h = _gmail_headers(full)
                sender  = _gmail_parse_sender(h.get("From", "невідомо"))
                subject = h.get("Subject", "без теми")
                lines.append(f"{sender}: «{subject}»")
            found += len(lines)
            prefix = f"{label} — " if multi else ""
            parts.append(prefix + "; ".join(lines))
        except Exception as e:
            log.error(f"Gmail search [{label}]: {e}")
    if not found:
        speak(f"По запиту «{query}» листів не знайдено.")
        return
    speak(f"Знайшла {vr.count(found, 'лист', 'листи', 'листів')} по «{query}»: " + ". ".join(parts) + ".")


# ============================================================
#  GOOGLE CALENDAR
# ============================================================
CALENDAR_TZ = "Europe/Vilnius"


def _calendar_agenda(which: str = "today") -> None:
    """Озвучує події: today / tomorrow / week."""
    svc = _get_calendar()
    if not svc:
        speak("Календар не налаштований.")
        return
    try:
        now = datetime.now()
        if which == "tomorrow":
            start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end   = start + timedelta(days=1)
            label = "Завтра"
        elif which == "week":
            start = now
            end   = now + timedelta(days=7)
            label = "На тиждень"
        else:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end   = start + timedelta(days=1)
            label = "Сьогодні"

        res = svc.events().list(
            calendarId="primary",
            timeMin=start.astimezone().isoformat(),
            timeMax=end.astimezone().isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=15,
        ).execute()
        items = res.get("items", [])
        if not items:
            speak(f"{label} подій немає.")
            return
        lines = []
        for ev in items[:6]:
            summary = ev.get("summary", "без назви")
            st = ev["start"].get("dateTime", ev["start"].get("date", ""))
            tstr = ""
            if "T" in st:
                try:
                    dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                    tstr = dt.strftime("%H:%M") + " "
                except Exception:
                    pass
            lines.append(f"{tstr}{summary}")
        speak(f"{label} у тебе {vr.count(len(items), 'подія', 'події', 'подій')}: " + "; ".join(lines) + ".")
        log.info(f"Calendar {which}: {len(items)} подій")
    except Exception as e:
        log.error(f"Calendar agenda: {e}")
        speak("Не вдалося прочитати календар.")


def _calendar_create(summary: str, start_str: str, minutes: int = 60) -> None:
    """Створює подію. start_str у форматі 'YYYY-MM-DD HH:MM'."""
    svc = _get_calendar()
    if not svc:
        speak("Календар не налаштований.")
        return
    try:
        start_dt = datetime.strptime(start_str.strip(), "%Y-%m-%d %H:%M")
        end_dt   = start_dt + timedelta(minutes=minutes)
        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": CALENDAR_TZ},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": CALENDAR_TZ},
        }
        svc.events().insert(calendarId="primary", body=body).execute()
        when = start_dt.strftime("%d.%m о %H:%M")
        speak(f"Додала в календар: «{summary}» на {when}.")
        log.info(f"Calendar create: {summary} @ {start_str}")
    except ValueError:
        speak("Не зрозуміла дату чи час події.")
    except Exception as e:
        log.error(f"Calendar create: {e}")
        speak("Не вдалося створити подію.")


# ============================================================
#  GMAIL — ПОВНЕ ЧИТАННЯ + ЧЕРНЕТКИ
# ============================================================

def _gmail_extract_body(msg: dict) -> str:
    """Витягує текстове тіло листа з payload (обходить MIME-частини)."""
    import base64

    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    payload = msg.get("payload", {})

    def _walk(part) -> str:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return _decode(body["data"])
        if mime.startswith("multipart") or part.get("parts"):
            for sub in part.get("parts", []):
                txt = _walk(sub)
                if txt:
                    return txt
        if mime == "text/html" and body.get("data"):
            import re as _re
            return _re.sub(r"<[^>]+>", " ", _decode(body["data"]))
        return ""

    text = _walk(payload)
    if not text and payload.get("body", {}).get("data"):
        text = _decode(payload["body"]["data"])
    return text.strip()


def _gmail_read_full(query: str = "") -> None:
    """Читає вголос повний текст листа (останнього непрочитаного або за запитом, з усіх скриньок)."""
    if not _gmail_accounts():
        speak("Gmail не налаштований.")
        return
    found = _gmail_find(query)
    if not found:
        speak("Такого листа не знайшла.")
        return
    label, svc, msg_id = found
    try:
        full = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        h = _gmail_headers(full)
        sender  = _gmail_parse_sender(h.get("From", "невідомо"))
        subject = h.get("Subject", "без теми")
        body = _gmail_extract_body(full)
        body = " ".join(body.split())   # прибираємо зайві пробіли/переноси

        if len(body) > 600:
            # Довгий лист — стискаємо через Groq
            try:
                resp = llm_chat(
                    GROQ_PRIMARY_MODEL,
                    messages=[{"role": "user", "content":
                        f"Перекажи КОРОТКО українською (2-3 речення) суть цього листа:\n\n{body[:2500]}"}],
                    max_tokens=180, temperature=0.3,
                )
                body = resp.choices[0].message.content.strip()
                speak(f"Лист від {sender}, тема «{subject}». Коротко: {body}")
                return
            except Exception:
                body = body[:500] + "…"
        speak(f"Лист від {sender}, тема «{subject}». {body}")
        log.info(f"Gmail read full: {subject}")
    except Exception as e:
        log.error(f"Gmail read full: {e}")
        speak("Не вдалося прочитати лист.")


def _gmail_draft_reply(query: str, reply_text: str) -> None:
    """Створює ЧЕРНЕТКУ відповіді на лист (не відправляє — для безпеки)."""
    if not _gmail_accounts():
        speak("Gmail не налаштований.")
        return
    if not reply_text.strip():
        speak("Що саме відповісти?")
        return
    found = _gmail_find(query)
    if not found:
        speak("Не знайшла на що відповідати.")
        return
    label, svc, msg_id = found
    try:
        import base64
        from email.message import EmailMessage

        orig = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"]
        ).execute()
        h = _gmail_headers(orig)
        # Відповідаємо туди, куди просить відправник (Reply-To), інакше на From
        to_addr = h.get("Reply-To") or h.get("From", "")
        subject = h.get("Subject", "")
        msg_id  = h.get("Message-ID", "")
        thread_id = orig.get("threadId")

        # EmailMessage, а не MIMEText: старий API кодував «Іван <ivan@x.com>»
        # цілком в один шматок, і адреса отримувача губилась, щойно імʼя
        # було кирилицею чи з литовськими літерами.
        mime = EmailMessage()
        mime["To"] = to_addr
        mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if msg_id:
            mime["In-Reply-To"] = msg_id
            mime["References"]  = f"{h.get('References', '')} {msg_id}".strip()
        mime.set_content(reply_text)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": thread_id}}
        ).execute()
        who = _gmail_parse_sender(to_addr)
        speak(f"Створила чернетку відповіді для {who}. Перевір у Gmail перед відправкою.")
        log.info(f"Gmail draft reply to {who}")
    except Exception as e:
        log.error(f"Gmail draft reply: {e}")
        speak("Не вдалося створити чернетку.")


# ============================================================
#  VISION — БАЧЕННЯ ЕКРАНУ
# ============================================================

def _capture_screen_b64(max_side: int = 1280, quality: int = 70) -> str:
    """Знімає екран, стискає і повертає base64 JPEG."""
    import base64, io
    img = pyautogui.screenshot()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _screen_look(question: str = "") -> None:
    """
    Дивиться на поточний екран через Groq vision-модель і озвучує.
    question — якщо задано, відповідає на конкретне питання про екран.
    """
    if not VISION_MODEL:
        # Groq прибрав llama-4-scout, іншої моделі із зображеннями там більше
        # немає (перевірено списком моделей 2026-08-15). Краще чесно сказати,
        # ніж падати з 404 на кожен запит про екран.
        log.warning("Screen look: vision-модель не налаштована")
        speak("Зараз не бачу екран: у Groq більше немає моделі із зображеннями.")
        return
    try:
        b64 = _capture_screen_b64()
        prompt = question.strip() or "Що зараз на екрані? Опиши коротко українською, одне-два речення."
        prompt += " Відповідай ТІЛЬКИ українською, без англійської."
        resp = llm_chat(
            VISION_MODEL,
            fallback=False,          # резервна gpt-oss на Groq картинок не бачить
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}],
            max_tokens=200,
            temperature=0.3,
        )
        answer = resp.choices[0].message.content.strip()
        log.info(f"Screen look: '{answer[:100]}'")
        speak(answer)
    except Exception as e:
        log.error(f"Screen look: {e}", exc_info=True)
        speak("Не вдалося подивитись на екран.")


# ============================================================
#  МОНІТОРИНГ — ПРОАКТИВНІ СПОВІЩЕННЯ
# ============================================================

def _gmail_check_new() -> list:
    """Повертає НОВІ непрочитані листи (label, sender, subject) з усіх скриньок."""
    accounts = _gmail_accounts()
    if not accounts:
        return []
    multi = len(accounts) > 1
    new_items = []
    listed = set()        # ключі листів, які зараз лежать у непрочитаних вхідних
    ok_accounts = set()   # скриньки, які цього разу вдалося перевірити
    try:
        for label, svc in accounts:
            try:
                box = _gmail_box(label)
                token_path = next((a["token"] for a in GMAIL_ACCOUNTS
                                   if a["label"] == label), label)
                # Мітки Трекер/* і є памʼяттю монітора: лист із такою міткою вже
                # розкладено (у минулому проході чи до перезапуску) і, якщо він
                # того вартий, уже оголошено. Памʼять у RAM цього не переживала.
                processed = set()
                if MAIL_TRIAGE_ENABLED and mail_triage:
                    try:
                        processed = set(_triage_labels(svc, token_path).values())
                    except Exception as e:
                        log.error(f"Мітки Трекер [{label}]: {e}")
                # 40, а не 10: сортувальник має встигати за напливом розсилок,
                # інакше вхідні знову заростуть між перевірками
                res = svc.users().messages().list(
                    userId="me", labelIds=["UNREAD", "INBOX"], maxResults=40
                ).execute()
                msgs = res.get("messages", [])
                for m in msgs:
                    key = f"{label}:{m['id']}"   # унікальний ключ на акаунт
                    listed.add(key)
                    if key in _seen_gmail_ids:
                        continue
                    _seen_gmail_ids[key] = True
                    full = svc.users().messages().get(
                        userId="me", id=m["id"], format="metadata",
                        metadataHeaders=["From", "Subject"]
                    ).execute()
                    if processed & set(full.get("labelIds", [])):
                        continue                  # уже розкладений раніше
                    h = _gmail_headers(full)
                    raw_from = h.get("From", "невідомо")
                    subject  = h.get("Subject", "без теми")

                    # Розкласти по мітках і прибрати шум із вхідних
                    cat = _gmail_triage(svc, token_path, m["id"], raw_from, subject, box)

                    # Голосом озвучуємо лише те, що того варте. Якщо сортування
                    # не спрацювало (cat is None), поводимось як раніше й кажемо
                    # про все: краще зайвий раз сказати, ніж мовчки проковтнути.
                    if cat is not None and cat not in mail_triage.ANNOUNCE:
                        continue

                    sender = _gmail_parse_sender(raw_from)
                    if multi:
                        sender = f"{sender} ({label})"
                    if cat:
                        sender = f"{sender}, {cat}"
                    new_items.append((m["id"], sender, subject))
                ok_accounts.add(label)
            except Exception as e:
                log.error(f"Gmail monitor [{label}]: {e}")
        # Забуваємо лише листи, яких уже немає в непрочитаних вхідних (прочитані,
        # заархівовані). Раніше при переповненні викидались НАЙСТАРІШІ ключі,
        # тобто якраз важливі непрочитані, і їх оголошувало вдруге як нові.
        for key in list(_seen_gmail_ids):
            if key.rsplit(":", 1)[0] in ok_accounts and key not in listed:
                del _seen_gmail_ids[key]
        return new_items
    except Exception as e:
        log.error(f"Gmail monitor check: {e}")
        return []


def _slack_check_new() -> list:
    """Повертає НОВІ згадки/DM (username, text) з останньої перевірки."""
    global _seen_slack_ts
    sl = _get_slack()
    if not sl:
        return []
    try:
        result = sl.search_messages(query="is:mention", count=10, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        new_items = []
        max_ts = _seen_slack_ts
        for m in matches:
            ts = float(m.get("ts", 0))
            if ts <= _seen_slack_ts:
                continue
            max_ts = max(max_ts, ts)
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            new_items.append((uname, text))
        _seen_slack_ts = max_ts
        return new_items
    except Exception as e:
        log.error(f"Slack monitor check: {e}")
        return []


def _monitor_toggle(target: str, on: bool) -> None:
    """Вмикає/вимикає монітор пошти, слаку або системи."""
    global SYS_MONITOR_ENABLED
    target = target.lower().strip()

    # Система — окремий прапорець
    if target in ("system", "система", "систему", "all", "все", "всі"):
        SYS_MONITOR_ENABLED = on
        if target in ("system", "система", "систему"):
            speak("Стежу за системою." if on else "Більше не стежу за системою.")
            return

    targets = ["gmail", "slack"] if target in ("all", "все", "всі") else [target]

    done = []
    for tg in targets:
        if tg not in _MONITOR:
            continue
        if on:
            # Перевіряємо що сервіс взагалі налаштований
            if tg == "gmail" and not _get_gmail():
                speak("Спершу налаштуй Gmail — потрібен файл credentials.")
                continue
            if tg == "slack" and not _get_slack():
                speak("Спершу налаштуй Slack — потрібен токен у конфігу.")
                continue
            _MONITOR[tg] = True
            _monitor_primed[tg] = False   # перший прохід запам'ятає поточний стан без спаму
        else:
            _MONITOR[tg] = False
        done.append(tg)

    if not done:
        return
    names = {"gmail": "пошта", "slack": "Slack"}
    label = " і ".join(names.get(d, d) for d in done)
    if on:
        speak(f"Стежу за {label}. Сповіщу коли щось нове.")
    else:
        speak(f"Більше не стежу за {label}.")


def monitor_loop():
    """Фоновий потік — періодично перевіряє пошту і Slack, оголошує нове."""
    # Монітор пошти вмикається сам, інакше автосортування не працює доти,
    # доки про нього не згадаєш і не тицьнеш у трей після кожного перезапуску.
    if MONITOR_GMAIL_AUTOSTART:
        _MONITOR["gmail"] = True
    log.info(f"Monitor loop запущено (пошта: {'увімкнена' if _MONITOR['gmail'] else 'вимкнена'}, "
             f"сортування: {'так' if MAIL_TRIAGE_ENABLED and mail_triage else 'ні'})")
    while True:
        time.sleep(MONITOR_INTERVAL)
        try:
            # ── Gmail ──
            if _MONITOR["gmail"]:
                new_mail = _gmail_check_new()
                first_pass = not _monitor_primed["gmail"]
                _monitor_primed["gmail"] = True
                if first_pass and not (MAIL_TRIAGE_ENABLED and mail_triage):
                    # Без міток памʼяті між запусками немає: перший прохід лише
                    # запамʼятовує, щоб не зачитати всі старі непрочитані.
                    # З мітками нерозкладене на старті справді нове (прийшло,
                    # поки компʼютер був вимкнений), і про нього варто сказати.
                    pass
                elif new_mail:
                    if len(new_mail) == 1:
                        _, sender, subj = new_mail[0]
                        speak(f"Новий лист від {sender}: «{subj}».")
                    else:
                        senders = ", ".join(s for _, s, _ in new_mail[:3])
                        speak(f"{vr.count(len(new_mail), 'новий лист', 'нові листи', 'нових листів')}, "
                              f"зокрема від {senders}.")

            # ── Slack ──
            if _MONITOR["slack"]:
                new_msgs = _slack_check_new()
                if not _monitor_primed["slack"]:
                    _monitor_primed["slack"] = True
                elif new_msgs:
                    if len(new_msgs) == 1:
                        uname, text = new_msgs[0]
                        speak(f"Slack — {uname} згадав тебе: {text}.")
                    else:
                        unames = ", ".join(u for u, _ in new_msgs[:3])
                        speak(f"{len(new_msgs)} нових згадок у Slack від {unames}.")
        except Exception as e:
            log.error(f"Monitor loop помилка: {e}")


# ============================================================
#  МОНІТОРИНГ СИСТЕМИ
# ============================================================

def _sys_alert(category: str, message: str) -> None:
    """Озвучує системне сповіщення з кулдауном (щоб не спамило)."""
    now = time.monotonic()
    last = _sys_alert_last.get(category, 0)
    if now - last < SYS_ALERT_COOLDOWN:
        return   # ще на кулдауні
    _sys_alert_last[category] = now
    log.warning(f"SYS ALERT [{category}]: {message}")
    speak(message)


def _check_resources() -> None:
    """Перевіряє CPU, RAM, диск, батарею, температуру і сповіщає при проблемах."""
    global _cpu_high_streak

    # ── CPU (стійке навантаження) ──
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        if cpu >= SYS_THRESHOLDS["cpu"]:
            _cpu_high_streak += 1
            if _cpu_high_streak >= SYS_CPU_STREAK:
                # знаходимо процес-винуватця
                top = max(psutil.process_iter(['name', 'cpu_percent']),
                          key=lambda p: p.info.get('cpu_percent') or 0, default=None)
                who = f" Найбільше вантажить {top.info['name']}." if top and top.info.get('name') else ""
                _sys_alert("cpu", f"Увага: процесор завантажений на {cpu:.0f} відсотків вже довго.{who}")
        else:
            _cpu_high_streak = 0
    except Exception as e:
        log.debug(f"CPU check: {e}")

    # ── RAM ──
    try:
        mem = psutil.virtual_memory()
        if mem.percent >= SYS_THRESHOLDS["ram"]:
            _sys_alert("ram", f"Увага: пам'ять заповнена на {mem.percent:.0f} відсотків. Закрий щось важке.")
    except Exception as e:
        log.debug(f"RAM check: {e}")

    # ── Диск C: ──
    try:
        disk = psutil.disk_usage("C:\\")
        free_gb = disk.free / 1024 ** 3
        if free_gb < SYS_THRESHOLDS["disk_free_gb"]:
            _sys_alert("disk", f"Увага: на диску C залишилось лише {free_gb:.1f} гігабайт.")
    except Exception as e:
        log.debug(f"Disk check: {e}")

    # ── Батарея ──
    try:
        batt = psutil.sensors_battery()
        if batt is not None and not batt.power_plugged and batt.percent <= SYS_THRESHOLDS["battery_low"]:
            _sys_alert("battery", f"Батарея низька — {batt.percent:.0f} відсотків. Постав на зарядку.")
    except Exception as e:
        log.debug(f"Battery check: {e}")

    # ── Температура (часто недоступна на Windows) ──
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            hottest = max((t.current for sensors in temps.values() for t in sensors
                           if t.current), default=0)
            if hottest >= SYS_THRESHOLDS["temp"]:
                _sys_alert("temp", f"Увага: висока температура — {hottest:.0f} градусів.")
    except Exception as e:
        log.debug(f"Temp check: {e}")


def _get_event_log_errors():
    """
    Читає журнал подій Windows (System) за час від останньої перевірки.
    Повертає список (provider, level, message) критичних/важливих помилок.
    """
    global _last_event_check
    now = datetime.now()
    since = _last_event_check or (now - timedelta(minutes=5))
    _last_event_check = now

    minutes = max(1, int((now - since).total_seconds() / 60) + 1)
    # Level 1 = Critical, 2 = Error
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-WinEvent -FilterHashtable @{{LogName='System'; Level=1,2; "
        f"StartTime=(Get-Date).AddMinutes(-{minutes})}} -MaxEvents 25 | "
        "Select-Object ProviderName, LevelDisplayName, Id, "
        "@{N='Msg';E={$_.Message.Substring(0,[Math]::Min(150,$_.Message.Length))}} | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="ignore",
        )
        out = (result.stdout or "").strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        events = []
        for ev in data:
            provider = (ev.get("ProviderName") or "").strip()
            level    = (ev.get("LevelDisplayName") or "").strip()
            msg      = (ev.get("Msg") or "").strip().replace("\n", " ").replace("\r", " ")
            plow = provider.lower()
            # Критичні — завжди; помилки — лише з важливих джерел
            is_critical = level.lower() in ("critical", "критичний")
            is_important_source = any(s in plow for s in SYS_EVENT_SOURCES)
            if is_critical or is_important_source:
                events.append((provider, level, msg))
        return events
    except subprocess.TimeoutExpired:
        log.debug("Event log check timeout")
        return []
    except Exception as e:
        log.debug(f"Event log check: {e}")
        return []


def _describe_event(provider: str, msg: str) -> str:
    """Перетворює технічну подію на коротке людське пояснення."""
    p = provider.lower()
    if "whea" in p:
        return "Апаратна помилка — можливо проблема з залізом."
    if "kernel-power" in p:
        return "Комп'ютер раптово вимкнувся минулого разу."
    if any(g in p for g in ("nvlddmkm", "amdkmdag", "igfx", "display")):
        return "Відеодрайвер дав збій."
    if any(d in p for d in ("disk", "ntfs", "volmgr", "volsnap")):
        return "Помилка диска — варто перевірити накопичувач."
    if "kernel-pnp" in p:
        return "Проблема з драйвером пристрою."
    if "bugcheck" in p:
        return "Був синій екран, система впала."
    return f"Збій у {provider}." if provider else "Системна помилка."


def sys_monitor_loop():
    """Фоновий потік — стежить за навантаженням і збоями системи."""
    global _last_event_check
    log.info("System monitor loop запущено")
    _last_event_check = datetime.now()   # baseline — старі події не оголошуємо
    cycle = 0
    while True:
        time.sleep(SYS_MONITOR_INTERVAL)
        if not SYS_MONITOR_ENABLED:
            continue
        try:
            _check_resources()
            cycle += 1
            if cycle >= SYS_EVENT_EVERY:
                cycle = 0
                events = _get_event_log_errors()
                if events:
                    # Беремо найважливішу подію (критичні першими)
                    ev = next((e for e in events if e[1].lower() in ("critical", "критичний")), events[0])
                    provider, level, msg = ev
                    human = _describe_event(provider, msg)
                    extra = (f" Ще {vr.count(len(events) - 1, 'подія', 'події', 'подій')} у журналі."
                             if len(events) > 1 else "")
                    _sys_alert(f"event_{provider.lower()}", f"Системне сповіщення: {human}{extra}")
        except Exception as e:
            log.error(f"System monitor помилка: {e}")


def _system_health_report() -> str:
    """Звіт про стан системи на вимогу: ресурси + останні помилки журналу."""
    parts = []
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        parts.append(f"процесор {cpu:.0f} відсотків")
        parts.append(f"пам'ять {mem.percent:.0f} відсотків")
        disk = psutil.disk_usage("C:\\")
        parts.append(f"на диску C вільно {disk.free / 1024**3:.0f} гігабайт")
        batt = psutil.sensors_battery()
        if batt is not None:
            st = "заряджається" if batt.power_plugged else "від батареї"
            parts.append(f"батарея {batt.percent:.0f} відсотків, {st}")
    except Exception as e:
        log.debug(f"Health report resources: {e}")

    status = "Все в нормі" if not parts else "Зараз: " + ", ".join(parts)

    # Останні помилки за 30 хв (без зміни baseline моніторингу)
    try:
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2; "
            "StartTime=(Get-Date).AddMinutes(-30)} -MaxEvents 5 | "
            "Measure-Object | Select-Object -ExpandProperty Count"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="ignore",
        )
        cnt = (result.stdout or "0").strip()
        if cnt.isdigit() and int(cnt) > 0:
            status += f". За останні пів години в журналі {vr.count(int(cnt), 'помилка', 'помилки', 'помилок')}"
        else:
            status += ". Помилок у журналі немає"
    except Exception as e:
        log.debug(f"Health report events: {e}")

    return status + "."


SHUTDOWN_DELAY = 30   # секунд до вимкнення/ребуту: час передумати

# Дія, що сама сказала про невдачу («Не знайшла програму»), ставить прапорець,
# і ask_lin тоді не зачитує текст моделі («Відкриваю…»): він був би неправдою.
_action_failed = threading.Event()


def _mark_action_failed():
    _action_failed.set()


def _cancel_shutdown():
    """shutdown /a і чесна відповідь, чи було що скасовувати."""
    try:
        r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, timeout=10)
        speak("Скасувала вимкнення." if r.returncode == 0 else
              "Вимкнення не було заплановане.")
    except Exception as e:
        log.error(f"shutdown /a: {e}")
        speak("Не вийшло скасувати. Набери shutdown /a вручну.")


def execute_action(action_str: str):
    try:
        inner = action_str.strip("[]").replace("ACTION:", "")
        parts = inner.split(":", 1)
        t = parts[0]
        p = parts[1].strip() if len(parts) > 1 else ""

        if   t == "open_app":          open_application(p)
        elif t == "search_web":
            # quote_plus: без нього «C# tutorial» Google отримував як «C»
            url = f"https://www.google.com/search?q={urllib.parse.quote_plus(p)}"
            _open_url(url)
            log.info(f"Chrome search: {url}")
        elif t == "open_youtube":
            url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(p)}"
                   if p else "https://youtube.com")
            _open_url(url)
            log.info(f"Chrome YouTube: {url}")
        elif t == "open_url":
            url = p if p.startswith("http") else f"https://{p}"
            _open_url(url)
            log.info(f"Chrome URL: {url}")
        elif t == "open_folder":
            folder_aliases = {
                "скріншот": SCREENSHOT_DIR, "скріншоти": SCREENSHOT_DIR,
                "робочий стіл": SCREENSHOT_DIR, "десктоп": SCREENSHOT_DIR,
                "документи": os.path.join(os.path.expanduser("~"), "Documents"),
                "завантаження": os.path.join(os.path.expanduser("~"), "Downloads"),
                "музика": os.path.join(os.path.expanduser("~"), "Music"),
                "відео": os.path.join(os.path.expanduser("~"), "Videos"),
                "зображення": os.path.join(os.path.expanduser("~"), "Pictures"),
            }
            target = next(
                (v for k, v in folder_aliases.items() if k in p.lower()),
                p if (p and os.path.exists(p)) else os.path.expanduser("~")
            )
            subprocess.Popen(["explorer", target])
        elif t == "get_time":          pass
        elif t == "volume_up":
            # Якщо Spotify грає — змінюємо його гучність, а не системну
            if not _maybe_spotify_volume("up"):
                volume_control("up")
        elif t == "volume_down":
            if not _maybe_spotify_volume("down"):
                volume_control("down")
        elif t == "volume_mute":       volume_control("mute")
        elif t == "screenshot":
            path = take_screenshot()
            if path:
                speak("Скріншот збережено.")
        elif t == "type_text":
            # pyautogui.write не підтримує Unicode — використовуємо clipboard
            import pyperclip as _pc
            _old = _pc.paste()
            _pc.copy(p)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
            _pc.copy(_old)   # відновлюємо буфер
        elif t == "hotkey":
            # Переклад українських назв клавіш в англійські
            uk_to_en = {
                "пробіл": "space", "ентер": "enter", "вхід": "enter",
                "вверх": "up", "вниз": "down", "вгору": "up",
                "ліво": "left", "право": "right",
                "ліворуч": "left", "праворуч": "right",
                "таб": "tab", "ескейп": "escape", "есс": "escape",
                "видалити": "delete", "назад": "backspace",
                "додому": "home", "кінець": "end",
                "плюс": "add", "мінус": "subtract",
            }
            keys = [uk_to_en.get(k.lower(), k.lower()) for k in p.split("+")]
            log.info(f"Hotkey: {keys}")
            pyautogui.hotkey(*keys)
        elif t == "close_window":      pyautogui.hotkey("alt", "F4")
        elif t == "minimize_all":      pyautogui.hotkey("win", "d")
        elif t == "focus_window":
            # Фокусує і розгортає вікно за назвою
            try:
                import pygetwindow as gw
                wins = [w for w in gw.getAllWindows() if p.lower() in w.title.lower() and w.title]
                if wins:
                    w = wins[0]
                    w.restore()
                    w.activate()
                    w.maximize()
                    log.info(f"Focused: {w.title}")
                else:
                    log.warning(f"Вікно '{p}' не знайдено")
            except Exception as e:
                log.error(f"focus_window помилка: {e}")
        elif t == "maximize_window":
            try:
                import pygetwindow as gw
                wins = [w for w in gw.getAllWindows() if p.lower() in w.title.lower() and w.title]
                if wins:
                    wins[0].maximize()
            except Exception as e:
                log.error(f"maximize_window помилка: {e}")
        elif t == "system_lock":       subprocess.Popen("rundll32.exe user32.dll,LockWorkStation")
        elif t == "system_sleep":
            subprocess.Popen(["powershell", "-c",
                "Add-Type -Assembly System.Windows.Forms; "
                "[System.Windows.Forms.Application]::SetSuspendState("
                "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"])
        # Вимкнення і ребут: після підтвердження в ask_lin і з запасом у 30 с,
        # за які можна сказати «скасуй вимкнення» (shutdown /a).
        elif t == "system_restart":
            subprocess.Popen(["shutdown", "/r", "/t", str(SHUTDOWN_DELAY)])
            speak(f"Перезавантажую через {SHUTDOWN_DELAY} секунд. Скажи «скасуй вимкнення», щоб зупинити.")
        elif t == "system_shutdown_pc":
            subprocess.Popen(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY)])
            speak(f"Вимикаю через {SHUTDOWN_DELAY} секунд. Скажи «скасуй вимкнення», щоб зупинити.")
        elif t == "system_cancel_shutdown":
            _cancel_shutdown()
        elif t == "kill_process":
            n = kill_process(p)
            speak(f"Закрила {p}." if n == 1 else
                  f"Закрила {p}: {vr.count(n, 'процес', 'процеси', 'процесів')}." if n else
                  f"Не знайшла процес «{p}».")

        # ── SPOTIFY ──
        elif t == "spotify_play":
            if p:
                # Спочатку пробуємо через API (пошук + відтворення)
                sp = _get_spotipy()
                played = False
                if sp:
                    try:
                        results = sp.search(q=p, type="track", limit=1)
                        tracks = results.get("tracks", {}).get("items", [])
                        if tracks:
                            sp.start_playback(uris=[tracks[0]["uri"]])
                            log.info(f"Spotify API play: {tracks[0]['name']}")
                            played = True
                    except Exception as e:
                        log.warning(f"Spotify API play failed: {e}")
                        if "No active device" in str(e):
                            speak("Відкрий Spotify на телефоні або ПК, щоб я могла відтворити.")
                            return
                if not played:
                    # Fallback: URI схема
                    uri = "spotify:search:" + urllib.parse.quote(p)
                    os.startfile(uri)   # без shell — ShellExecute по протоколу spotify:
                    log.info(f"Spotify URI fallback: {uri}")
            else:
                _spotify_set_playing(True)      # «грай» без назви = продовжити

        elif t == "spotify_pause":
            _spotify_set_playing(False)

        elif t == "spotify_resume":
            _spotify_set_playing(True)

        elif t == "spotify_next":
            sp = _get_spotipy()
            if sp:
                try:
                    sp.next_track(); log.info("Spotify API: next")
                except Exception as e:
                    log.warning(f"Spotify next API fail: {e}"); pyautogui.hotkey("nexttrack")
            else:
                pyautogui.hotkey("nexttrack")

        elif t == "spotify_prev":
            sp = _get_spotipy()
            if sp:
                try:
                    sp.previous_track(); log.info("Spotify API: prev")
                except Exception as e:
                    log.warning(f"Spotify prev API fail: {e}"); pyautogui.hotkey("prevtrack")
            else:
                pyautogui.hotkey("prevtrack")

        elif t == "spotify_stop":
            _spotify_set_playing(False)

        elif t == "spotify_device":
            _spotify_transfer_device(p)

        elif t == "spotify_devices":
            _spotify_list_devices()

        elif t == "spotify_current":
            _spotify_now_playing()

        elif t == "spotify_recent":
            n = int(p) if p.isdigit() else 5
            _spotify_recent(n)

        elif t == "spotify_liked":
            n = int(p) if p.isdigit() else 5
            _spotify_liked(n)

        elif t == "spotify_volume_up":
            step = int(p) if p.isdigit() else 10
            _spotify_volume("up", step)

        elif t == "spotify_volume_down":
            step = int(p) if p.isdigit() else 10
            _spotify_volume("down", step)

        elif t == "spotify_volume":
            # p = "50" → встановити 50%
            try:
                _spotify_volume("set", int(p))
            except ValueError:
                speak("Не зрозуміла рівень гучності.")

        elif t == "spotify_shuffle":
            # p = "on"/"off"/"toggle"
            if p in ("on", "увімкни", "так", "1", "true"):
                _spotify_shuffle(True)
            elif p in ("off", "вимкни", "ні", "0", "false"):
                _spotify_shuffle(False)
            else:
                # toggle — дивимось поточний стан
                sp2 = _get_spotipy()
                if sp2:
                    try:
                        st = sp2.current_playback()
                        current = st.get("shuffle_state", False) if st else False
                        _spotify_shuffle(not current)
                    except Exception:
                        _spotify_shuffle(True)
                else:
                    _spotify_shuffle(True)

        elif t == "spotify_repeat":
            # p = "track" | "context" | "off" | "трек" | "плейлист" тощо
            _spotify_repeat(p or "off")

        elif t == "spotify_playlists":
            threading.Thread(target=_spotify_playlists, daemon=True).start()

        elif t == "spotify_play_playlist":
            threading.Thread(
                target=lambda: _spotify_play_playlist(p), daemon=True
            ).start()

        elif t == "spotify_queue":
            threading.Thread(target=_spotify_queue, daemon=True).start()

        elif t == "spotify_playlist_tracks":
            threading.Thread(
                target=lambda: _spotify_playlist_tracks(p), daemon=True
            ).start()

        elif t == "spotify_add_queue":
            threading.Thread(
                target=lambda: _spotify_add_to_queue(p), daemon=True
            ).start()

        # ── НОТАТКИ ──
        elif t == "note_add":
            # формат p: "текст нотатки" або "текст|60" (з нагадуванням через 60 хв)
            parts_n = p.split("|", 1)
            txt = parts_n[0].strip()
            mins = int(parts_n[1]) if len(parts_n) > 1 and parts_n[1].strip().isdigit() else 0
            result = note_add(txt, mins)
            speak(result)
            return result

        elif t == "note_remind":
            # формат: "текст|30" (хвилини), "текст|21:00" (сьогодні/завтра)
            # або "текст|2026-09-26 09:00" (конкретна дата)
            parts_n = p.split("|", 1)
            txt = parts_n[0].strip()
            time_val = parts_n[1].strip() if len(parts_n) > 1 else "30"
            result = note_add(txt, remind_at_str=time_val)
            speak(result)
            return result

        elif t == "note_list":
            result = note_list()
            speak(result)
            return result

        elif t == "note_done":
            try:
                result = note_done(int(p))
            except ValueError:
                result = "Невірний номер нотатки."
            speak(result)
            return result

        elif t == "note_delete":
            try:
                result = note_delete(int(p))
            except ValueError:
                result = "Невірний номер нотатки."
            speak(result)
            return result

        elif t == "note_clear":
            result = note_clear("done" if p.strip().lower() in ("done", "виконані", "виконане") else "all")
            speak(result)
            return result

        # ── ДРУГИЙ МОЗОК ──
        elif t == "brain_add":
            result = brain_capture(p)
            speak(result)
            return result

        elif t == "brain_plan":
            result = brain_capture(p, "плани")
            speak(result)
            return result

        elif t == "brain_idea":
            result = brain_capture(p, "ідеї")
            speak(result)
            return result

        elif t == "brain_ask":
            brain_ask(p)          # сам скаже відповідь, коли прочитає нотатки
            return ""

        elif t == "brain_read":
            result = brain_read(p)
            speak(result)
            return result

        elif t == "dictate_type":
            if p.strip():
                _type_at_cursor(p.strip())    # текст названо одразу
            else:
                _dictate_to_cursor()          # диктуватиме окремо
            return ""

        elif t == "dictate_last":
            # «що я диктував», «скопіюй те, що я диктував», «повтори диктовку»
            if not _last_dictation:
                speak("Я ще нічого не диктувала.")
                return ""
            try:
                import pyperclip
                pyperclip.copy(_last_dictation)
            except Exception as e:
                log.error(f"dictate_last: {e}")
            speak("Остання диктовка знову в буфері.")
            return _last_dictation

        elif t == "dictate_to_brain":
            # «збережи диктовку в мозок» — якщо вставити було нікуди
            if not _last_dictation:
                speak("Я ще нічого не диктувала.")
                return ""
            result = brain_capture(_last_dictation)
            speak(result if len(_last_dictation) < 120 else "Зберегла у вхідні.")
            return result

        # ── ФОКУС ──
        elif t == "focus_start":
            mins = int(p) if p.isdigit() else 25
            result = focus_start(mins)
            speak(result)
            return result

        elif t == "focus_stop":
            result = focus_stop()
            speak(result)
            return result

        # ── ПАМ'ЯТЬ ──
        elif t == "memory_save_name":
            mem = load_memory()
            mem["user_name"] = p
            save_memory(mem)
            result = f"Запам'ятала — тебе звуть {p}."
            speak(result)
            return result

        elif t == "memory_add_fact":
            mem = load_memory()
            facts = mem.get("facts", [])
            facts.append(p)
            mem["facts"] = facts[-10:]  # зберігаємо останні 10 фактів
            save_memory(mem)
            log.info(f"Факт збережено: {p}")
            return f"Запам'ятала: {p}."

        elif t == "notepad_write":
            _notepad_write(p)
            return

        # ── ТАЙМЕР ──
        elif t == "timer":
            # p може бути "600", "10m", "1.5m", "10:30"
            seconds = 0
            lp = p.strip().lower()
            try:
                if lp.endswith("m"):
                    seconds = round(float(lp[:-1]) * 60)
                elif lp.endswith("s"):
                    seconds = round(float(lp[:-1]))
                elif ":" in lp:
                    parts_t = lp.split(":")
                    seconds = int(parts_t[0]) * 60 + int(parts_t[1])
                else:
                    seconds = round(float(lp))
            except (ValueError, IndexError):
                seconds = 0
            if seconds > 0:
                result = _timer_start(seconds)
                speak(result)
            else:
                speak("Не зрозуміла скільки часу.")
            return

        elif t == "timer_stop":
            speak(_timer_stop_all())
            return

        # ── БУФЕР ОБМІНУ ──
        elif t == "clipboard_read":
            threading.Thread(
                target=lambda: speak(_clipboard_read()), daemon=True
            ).start()
            return

        elif t == "clipboard_save":
            speak(_clipboard_save())
            return

        elif t == "clipboard_copy":
            speak(_clipboard_set(p))
            return

        # ── ШВИДКІСТЬ ГОЛОСУ ──
        elif t == "voice_faster":
            speak(_adjust_voice_rate("faster"))
            return

        elif t == "voice_slower":
            speak(_adjust_voice_rate("slower"))
            return

        elif t == "voice_reset":
            speak(_adjust_voice_rate("reset"))
            return

        # ── ВІКНА ──
        elif t == "window_snap":
            snap_map = {
                "left":      ("win", "left"),
                "ліво":      ("win", "left"),
                "ліворуч":   ("win", "left"),
                "right":     ("win", "right"),
                "право":     ("win", "right"),
                "праворуч":  ("win", "right"),
                "max":       ("win", "up"),
                "макс":      ("win", "up"),
                "повний":    ("win", "up"),
                "full":      ("win", "up"),
                "maximize":  ("win", "up"),
                "розгорнути":("win", "up"),
                "min":       ("win", "down"),
                "згорнути":  ("win", "down"),
                "minimize":  ("win", "down"),
            }
            key_val = p.lower().strip()
            keys = snap_map.get(key_val)
            if not keys:
                # fuzzy fallback
                close = difflib.get_close_matches(key_val, snap_map.keys(), n=1, cutoff=0.6)
                if close:
                    keys = snap_map[close[0]]
                    log.info(f"Window snap fuzzy: '{key_val}' → '{close[0]}'")
            if keys:
                pyautogui.hotkey(*keys)
                log.info(f"Window snap: {p}")
            else:
                log.warning(f"Невідомий snap: {p}")
            return

        # ── ВАЛЮТА ──
        elif t == "currency":
            # формат: "100:USD:UAH"
            parts_c = p.split(":")
            if len(parts_c) == 3:
                try:
                    amt = float(parts_c[0])
                    threading.Thread(
                        target=lambda: speak(_currency_convert(amt, parts_c[1], parts_c[2])),
                        daemon=True
                    ).start()
                except ValueError:
                    speak("Не зрозуміла суму для конвертації.")
            else:
                speak("Не зрозумів формат конвертації.")
            return

        # ── ПОГОДА ──
        # HTTP запит робиться паралельно; _tts_lock гарантує що погода
        # озвучується після того як AI-відповідь договорить
        elif t == "weather":
            _city = p if p else None
            threading.Thread(
                target=lambda: speak(_get_weather(_city)), daemon=True
            ).start()
            return

        # ── СТАН СИСТЕМИ ──
        elif t == "system_info":
            threading.Thread(
                target=lambda: speak(_get_system_info()), daemon=True
            ).start()
            return

        elif t == "system_health":
            threading.Thread(
                target=lambda: speak(_system_health_report()), daemon=True
            ).start()
            return

        elif t == "web_search":
            _silent_web_search(p)
            return

        elif t == "claude_session":
            summary = _read_claude_session(int(p) if p.isdigit() else 4)
            speak(summary)
            return summary

        elif t == "claude_read":
            summary = _read_claude_session(n_messages=6, keyword=p)
            speak(summary)
            return summary

        elif t == "claude_list":
            summary = _list_claude_sessions()
            speak(summary)
            return summary

        elif t == "ask_claude":
            _ask_claude_code(p)

        elif t == "ask_claude_web":
            _ask_claude_web(p)

        # ── SLACK ──
        elif t == "slack_unread":
            threading.Thread(target=_slack_unread, daemon=True).start()
            return

        elif t == "slack_dm":
            threading.Thread(target=_slack_dm, daemon=True).start()
            return

        elif t == "slack_mentions":
            threading.Thread(target=_slack_mentions, daemon=True).start()
            return

        elif t == "slack_channel":
            # p = назва каналу
            threading.Thread(
                target=lambda: _slack_channel(p), daemon=True
            ).start()
            return

        # ── GMAIL ──
        elif t == "gmail_unread":
            threading.Thread(target=_gmail_unread, daemon=True).start()
            return

        elif t == "gmail_latest":
            threading.Thread(target=_gmail_latest, daemon=True).start()
            return

        elif t == "gmail_search":
            # p = пошуковий запит
            threading.Thread(
                target=lambda: _gmail_search(p), daemon=True
            ).start()
            return

        elif t == "gmail_read_full":
            # p = опціональний запит (інакше останній непрочитаний)
            threading.Thread(
                target=lambda: _gmail_read_full(p), daemon=True
            ).start()
            return

        elif t == "gmail_reply":
            # формат: "запит|текст відповіді"  або просто "текст" (на останній непрочитаний)
            if "|" in p:
                q, body = p.split("|", 1)
            else:
                q, body = "", p
            threading.Thread(
                target=lambda: _gmail_draft_reply(q.strip(), body.strip()), daemon=True
            ).start()
            return

        # ── КАЛЕНДАР ──
        elif t == "calendar_today":
            threading.Thread(target=lambda: _calendar_agenda("today"), daemon=True).start()
            return

        elif t == "calendar_tomorrow":
            threading.Thread(target=lambda: _calendar_agenda("tomorrow"), daemon=True).start()
            return

        elif t == "calendar_week":
            threading.Thread(target=lambda: _calendar_agenda("week"), daemon=True).start()
            return

        elif t == "calendar_create":
            # формат: "Назва|YYYY-MM-DD HH:MM|хвилини"
            parts_e = p.split("|")
            if len(parts_e) >= 2:
                summary = parts_e[0].strip()
                when    = parts_e[1].strip()
                mins    = int(parts_e[2]) if len(parts_e) > 2 and parts_e[2].strip().isdigit() else 60
                threading.Thread(
                    target=lambda: _calendar_create(summary, when, mins), daemon=True
                ).start()
            else:
                speak("Не зрозуміла деталі події.")
            return

        # ── ЕКРАН (VISION) ──
        elif t == "screen_look":
            # p = опціональне питання про екран
            threading.Thread(
                target=lambda: _screen_look(p), daemon=True
            ).start()
            return

        # ── МОНІТОРИНГ ──
        elif t == "monitor_on":
            _monitor_toggle(p or "all", True)
            return

        elif t == "monitor_off":
            _monitor_toggle(p or "all", False)
            return

        elif t == "shutdown":
            speak("До побачення!")
            _mark_stop()
            os._exit(0)

        log.info(f"Дія виконана: [{t}:{p}]")
    except Exception as e:
        log.error(f"Помилка дії '{action_str}': {e}", exc_info=True)


# ============================================================
#  TTS
# ============================================================

_MD_RE = [
    (re.compile(r"```.*?```", re.S), " "),        # блоки коду не читаємо взагалі
    (re.compile(r"`([^`]*)`"), r"\1"),            # `код` → код
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),      # **жирне** → жирне
    (re.compile(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)"), r"\1"),   # *курсив* → курсив
    (re.compile(r"^#{1,6}\s*", re.M), ""),        # заголовки
    (re.compile(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]"), r"\1"),    # [[вікі-посилання]]
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),           # [текст](посилання)
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),      # маркери списків
    (re.compile(r"[ \t]{2,}"), " "),
]


def _strip_md(text: str) -> str:
    """
    Прибирає розмітку перед озвученням. Модель часто відповідає у Markdown,
    і зірочки з лапками або читаються вголос, або калічать інтонацію.
    На екран текст іде як є, чиститься лише те, що йде в голос.
    """
    for rx, rep in _MD_RE:
        text = rx.sub(rep, text)
    return text.strip()


# ── Кеш коротких фраз ─────────────────────────────────────────────────────────
# «Слухаю.», «Скасовую.», привітання звучать десятки разів на день, і щоразу
# чекати синтез edge-tts (запит у мережу) немає сенсу. Короткі фрази лежать
# готовими mp3. Тека поза проєктом, щоб не синхронізувалась в OneDrive.
TTS_CACHE_DIR       = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                                   "raphael-tts-cache")
TTS_CACHE_MAX_CHARS = 80
TTS_CACHE_MAX_FILES = 400


def _tts_cache_path(text: str) -> str | None:
    if len(text) > TTS_CACHE_MAX_CHARS:
        return None
    key = hashlib.sha1(f"{VOICE}|{VOICE_RATE}|{VOICE_PITCH}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(TTS_CACHE_DIR, key + ".mp3")


def _tts_cache_trim():
    """Лишає TTS_CACHE_MAX_FILES найсвіжіших файлів (за часом останнього використання)."""
    try:
        files = [os.path.join(TTS_CACHE_DIR, f) for f in os.listdir(TTS_CACHE_DIR)
                 if f.endswith(".mp3")]
        if len(files) <= TTS_CACHE_MAX_FILES:
            return
        files.sort(key=os.path.getmtime)
        for p in files[:len(files) - TTS_CACHE_MAX_FILES + 50]:
            os.remove(p)
    except Exception as e:
        log.debug(f"TTS кеш: {e}")


def _tts_synth_to(path: str, text: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(edge_tts.Communicate(
            text, voice=VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH).save(path))
    finally:
        loop.close()


def _tts_play(path: str):
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    clock = pygame.time.Clock()
    while pygame.mixer.music.get_busy():
        if _tts_stop.is_set():
            pygame.mixer.music.stop()
            log.debug("TTS: зупинено користувачем")
            break
        clock.tick(10)
    pygame.mixer.music.unload()


def speak(text: str):
    global _last_spoken, _tts_epoch, _tts_last_end
    if not text or not text.strip():
        return
    text = _strip_md(text)
    if not text:
        return

    _last_spoken = text   # зберігаємо для "повтори"
    log.debug(f"TTS: '{text[:50]}...' " if len(text) > 50 else f"TTS: '{text}'")
    _tts_stop.clear()
    if LIN_UI:
        LIN_UI.safe_set_state("speaking", text)
    with _tts_lock:  # один потік говорить за раз
        _tts_epoch += 1
        _tts_active.set()
        tmp = None
        try:
            cached = _tts_cache_path(text)
            if cached and os.path.exists(cached):
                path = cached
                try:
                    os.utime(cached, None)          # свіжий для _tts_cache_trim
                except Exception:
                    pass
            elif cached:
                os.makedirs(TTS_CACHE_DIR, exist_ok=True)
                tmp = cached + ".part"              # недописаний файл не потрапить у кеш
                _tts_synth_to(tmp, text)
                os.replace(tmp, cached)
                path, tmp = cached, None
                _tts_cache_trim()
            else:
                fd, tmp = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                _tts_synth_to(tmp, text)
                path = tmp
            _tts_play(path)
            log.debug("TTS: завершено")
        except Exception as e:
            log.error(f"TTS помилка: {e}", exc_info=True)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            _tts_last_end = time.monotonic()
            _tts_active.clear()
    if LIN_UI:
        LIN_UI.safe_set_state("idle")


# ============================================================
#  STT
# ============================================================

# Підказки для Whisper — часто вживані слова, щоб краще розпізнавав
_WHISPER_PROMPT = (
    "Рафаель, Рафа, Лін, відкрий, закрий, відкрити, закрити, Spotify, спотіфай, Discord, дискорд, "
    "Chrome, хром, Telegram, телеграм, VS Code, Blender, погода, таймер, нагадай, "
    "нотатки, відтворити, пауза, наступний, попередній, гучніше, тихіше, "
    "скріншот, заблокуй, вимкни, перезавантаж, що грає, буфер, скопіюй"
)

# Відомі галюцинації Whisper на тиші/музиці/шумі (субтитри з YouTube).
# Коли немає мови, модель «домальовує» саме ці фрази — ігноруємо їх.
_WHISPER_HALLUCINATIONS = {
    "дякую", "дякую за перегляд", "дякую за увагу", "дякуємо за перегляд",
    "дякую що дивитесь", "субтитри", "субтитрував", "субтитри створив",
    "субтитри від", "редактор субтитрів", "продовження далі", "продовження буде далі",
    "продовження слідує", "далі буде", "до зустрічі", "до нових зустрічей",
    "побачимось", "ще побачимось", "на все добре", "на цьому все",
    "підписуйтесь на канал", "підписуйтесь", "не забудьте підписатися",
    # рос. варіанти (Whisper плутає мови на шумі)
    "спасибо за просмотр", "продолжение следует", "субтитры", "субтитры сделал",
    "редактор субтитров", "спасибо за внимание", "подписывайтесь на канал",
}


_VOWELS = set("аеиоуяюєїіыэё")


def _is_noise(text: str) -> bool:
    """True якщо текст — галюцинація Whisper або суцільний шум (не команда)."""
    t = text.strip().strip("!?.,…\"'» «").lower()
    if not t:
        return True
    if t in _WHISPER_HALLUCINATIONS:
        return True
    compact = t.replace(" ", "")
    # Одна літера повторена ('шшшшшш', 'аааа') — будь-якої довжини
    if len(set(compact)) == 1 and len(compact) >= 3:
        return True
    # Короткий уривок без жодної голосної — майже завжди шум ('сьш', 'кхм', 'тлс', 'м-м-м', 'шш').
    # Реальні слова/вигуки (стоп, грай, далі, так, ні, ну, ок, ага) голосну містять — не чіпаємо.
    letters = re.sub(r"[^а-яёіїєґ']", "", compact)
    if 0 < len(letters) <= 5 and not (set(letters) & _VOWELS):
        return True
    return False


def _transcribe_whisper(audio) -> str:
    """Groq Whisper — набагато краща розпізнавання української ніж Google STT."""
    try:
        import io
        wav_bytes = audio.get_wav_data()
        result = client.audio.transcriptions.create(
            file=("audio.wav", io.BytesIO(wav_bytes)),
            model="whisper-large-v3-turbo",   # 28 800 сек/день — вистачить
            language="uk",
            response_format="text",
            prompt=_WHISPER_PROMPT,
            temperature=0.0,   # детерміновано — менше «домальованих» фраз на шумі
        )
        text = (result or "").strip().lower()
        if _is_noise(text):
            log.debug(f"Whisper галюцинація/шум — ігнорую: '{text}'")
            return ""
        if text:
            log.info(f"Whisper STT: '{text}'")
        return text
    except Exception as e:
        log.warning(f"Whisper STT failed, fallback to Google: {e}")
        return ""


def _transcribe_google(audio) -> str:
    """Google STT — резерв якщо Whisper не відповів."""
    try:
        text = recognizer.recognize_google(audio, language="uk-UA").lower()
        if _is_noise(text):
            log.debug(f"Google шум — ігнорую: '{text}'")
            return ""
        log.info(f"Google STT (fallback): '{text}'")
        return text
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        log.warning(f"Google STT failed: {e}")
        return ""


# ── Vosk: офлайн-розпізнавання (працює без інтернету) ─────────────────────────
VOSK_MODEL_PATH = os.path.join(SCRIPT_DIR, "vosk-model-uk")
_vosk_model = None
_vosk_failed = False   # якщо модель не завантажилась — не пробуємо знову (інакше спам у логах)


def _get_vosk_model():
    """Лінива ініціалізація Vosk-моделі (один раз). Провал кешується."""
    global _vosk_model, _vosk_failed
    if _vosk_model is not None:
        return _vosk_model
    if _vosk_failed:
        return None

    path = os.environ.get("LIN_VOSK_PATH") or VOSK_MODEL_PATH
    if not os.path.isdir(path):
        _vosk_failed = True
        log.warning(f"Vosk: модель не знайдено ({path}) — офлайн STT вимкнено")
        return None

    # Vosk/Kaldi (C++) не відкриває не-ASCII шляхи на Windows (тут «Документи»).
    # Якщо в шляху є кирилиця — робимо junction на ASCII-локацію (один раз).
    if not path.isascii():
        try:
            ascii_dir = os.path.join(
                os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "lin-vosk")
            if not os.path.isdir(ascii_dir):
                subprocess.run(["cmd", "/c", "mklink", "/J", ascii_dir, path],
                               capture_output=True, text=True, timeout=10)
            if os.path.isdir(ascii_dir) and ascii_dir.isascii():
                log.info(f"Vosk: ASCII-junction {ascii_dir} → модель")
                path = ascii_dir
        except Exception as e:
            log.debug(f"Vosk junction не створено: {e}")

    try:
        from vosk import Model
        _vosk_model = Model(path)
        log.info("Vosk модель завантажена (офлайн STT готовий)")
        return _vosk_model
    except Exception as e:
        _vosk_failed = True   # більше не пробуємо до перезапуску
        log.error(f"Vosk init (офлайн STT вимкнено до рестарту): {e}")
        return None


def _transcribe_vosk(audio) -> str:
    """Офлайн-розпізнавання через Vosk — останній резерв коли немає інтернету."""
    model = _get_vosk_model()
    if not model:
        return ""
    try:
        import json as _json
        from vosk import KaldiRecognizer
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        rec = KaldiRecognizer(model, 16000)
        rec.AcceptWaveform(raw)
        res = _json.loads(rec.FinalResult())
        text = (res.get("text") or "").strip().lower()
        if _is_noise(text):
            return ""
        if text:
            log.info(f"Vosk STT (офлайн): '{text}'")
        return text
    except Exception as e:
        log.warning(f"Vosk STT failed: {e}")
        return ""


# Експериментальний локальний фільтр імені (вимкнено за замовчуванням, вмикається
# в config.json). У normal-режимі кожна почута поруч фраза (телевізор, музика,
# дзвінок) іде в Groq Whisper, хоча потрібні лише ті, де є імʼя. З фільтром
# спершу Vosk НА ЦЬОМУ компʼютері перевіряє, чи схоже, що звернулись до Рафаеля,
# і тільки тоді аудіо летить у хмару. Без моделі Vosk фільтр нічого не блокує.
LOCAL_WAKE_GATE = False


def _vosk_heard_wake(audio) -> bool:
    model = _get_vosk_model()
    if not model:
        return True
    try:
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, 16000)
        rec.AcceptWaveform(audio.get_raw_data(convert_rate=16000, convert_width=2))
        heard = (json.loads(rec.FinalResult()).get("text") or "").strip().lower()
    except Exception as e:
        log.debug(f"Wake-фільтр Vosk: {e}")
        return True                       # фільтр зламався: краще пропустити, ніж оглухнути
    words = heard.split()
    edge = words[:vr.WAKE_MAX_POSITION] + words[-1:]
    ok = bool(vr.find_wake(heard, WAKE_WORDS)) or any(
        difflib.get_close_matches(w, WAKE_WORDS, n=1, cutoff=0.75) for w in edge)
    log.debug(f"Wake-фільтр Vosk: '{heard}' → {'пропускаю в Whisper' if ok else 'не імʼя'}")
    return ok


def _wait_tts_quiet():
    """Чекає, доки замовкне озвучка (плюс хвіст на відлуння кімнати)."""
    while _tts_active.is_set() or time.monotonic() - _tts_last_end < TTS_ECHO_TAIL:
        time.sleep(0.05)


_mic_calibrated = False   # калібруємо мікрофон лише раз

def listen(timeout=30, phrase_limit=20, passive=False, wake_gate=False) -> str:
    """
    Слухає одну фразу і повертає текст ("" якщо тиша чи не розпізнано).

    Поки говорить сам Рафаель, мікрофон не пише, а запис, під час якого
    почалась озвучка з фонового потоку (новий лист, таймер, нагадування),
    викидається. Інакше він чув сам себе, а тему листа «Рафаель, вимкни
    компʼютер» сприймав би як команду.

    passive=True: фонове слухання в normal-режимі, коли чекаємо імʼя. Стан
    вікна не змінюємо: «Слухаю» світиться лише тоді, коли Рафаель справді
    чекає команду, і на екран не виводиться все, що сказали поруч.
    wake_gate=True: з LOCAL_WAKE_GATE фраза без імені відкидається локально.
    """
    global _mic_calibrated
    show = bool(LIN_UI) and not passive
    if show:
        LIN_UI.safe_set_state("listening")
    try:
        while True:
            _wait_tts_quiet()
            epoch = _tts_epoch
            with sr.Microphone() as source:
                # Калібрування лише першого разу — далі dynamic_energy_threshold
                # сам підлаштовується. Це прибирає ~150мс затримки на кожній команді.
                if not _mic_calibrated:
                    recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    _mic_calibrated = True
                try:
                    audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
                except sr.WaitTimeoutError:
                    log.debug("STT: тайм-аут")
                    if show: LIN_UI.safe_set_state("idle")
                    return ""
            if _tts_active.is_set() or _tts_epoch != epoch:
                log.debug("STT: запис наклався на озвучку, відкидаю і слухаю знову")
                continue
            break

        if wake_gate and LOCAL_WAKE_GATE and not _vosk_heard_wake(audio):
            return ""

        # Whisper (найкраще) → Google → Vosk (офлайн, коли немає інтернету)
        text = (_transcribe_whisper(audio)
                or _transcribe_google(audio)
                or _transcribe_vosk(audio))

        if not text:
            log.debug("STT: не розпізнано")
            if show: LIN_UI.safe_set_state("idle")
            return ""

        if show:
            LIN_UI.safe_set_state("thinking", text)
        return text

    except Exception as e:
        log.error(f"listen() помилка: {e}", exc_info=True)
        if show: LIN_UI.safe_set_state("idle")
        time.sleep(1)   # без мікрофона цикл інакше крутився б без паузи, забиваючи лог
        return ""


# ============================================================
#  GROQ
# ============================================================

def inject_time(messages):
    now = datetime.now()
    days = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
    ts = f"Зараз {now.strftime('%H:%M')}, {days[now.weekday()]}, {now.strftime('%d.%m.%Y')}."
    result = list(messages)  # shallow copy
    # Додаємо час до вже оновленого build_system_prompt(), а не до старої константи
    result[0] = {"role": "system", "content": messages[0]["content"] + f" {ts}"}
    return result


# Слова що чітко вказують на команду-дію → можна швидкою моделлю 8b
_COMMAND_HINTS = (
    "відкрий", "відкрити", "закрий", "закрити", "запусти", "увімкни", "вимкни",
    "постав", "грай", "відтвори", "пауза", "зупини", "наступн", "попередн",
    "гучніше", "тихіше", "гучність", "голосніше", "тихше", "звук",
    "погода", "таймер", "нагадай", "скріншот", "заблокуй", "перезавантаж",
    "спотіфай", "spotify", "дискорд", "телеграм", "хром", "браузер",
    "що на екрані", "сховай", "згорни", "розгорни", "перемкни",
    "що в пошті", "плейлист", "шафл", "перемішай", "стеж за",
)


# Теми, де 8b помиляється дорого: або треба розібрати параметри (текст|час),
# або дія незворотна. 2026-08-15: «нагадай, через годину зробити перерву»
# пішло на 8b і повернулось як [note_done:3] — спроба закрити чужу нотатку.
_FORCE_PRIMARY = (
    "нагада", "нагадува",                      # note_remind з розбором часу
    "запиши", "занотуй", "нотатк", "план",     # note_add / brain_add
    "мозок", "вхідні", "обсідіан", "нотатц", "сховищ",
    "пиши", "друкуй", "надрукуй", "записуй",
    "видали", "видалит", "очисти", "прибери",  # незворотне
    "виконан", "зроблен", "готово",
)


# ── Провайдери LLM ───────────────────────────────────────────────────────────
# Groq, Gemini і локальні сервери (Ollama, LM Studio, llama.cpp) говорять ОДНИМ
# протоколом — OpenAI-сумісним. Тому провайдер це просто base_url + ключ, а не
# окремий SDK і окрема гілка коду на кожного.
#
# Модель пишеться як "провайдер:модель". Без префікса — Groq, щоб старі
# конфіги працювали без правок.
#   groq:openai/gpt-oss-120b   gemini:gemini-2.5-flash   local:llama3.2
LLM_PROVIDERS = {
    "groq":   {"base_url": "https://api.groq.com/openai/v1",
               "key_name": "GROQ_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_name": "GEMINI_API_KEY"},
    # Місце під майбутню локальну модель. Ollama слухає 11434, LM Studio 1234.
    "local":  {"base_url": "http://127.0.0.1:11434/v1",
               "key_name": "", "key_default": "ollama"},
}
_llm_clients = {}


# Таймаут одного запиту до моделі. Повтори SDK вимкнені (max_retries=0): за
# замовчуванням openai робить ще 2 спроби, і на мертвому провайдері це до
# півтори хвилини тиші. Швидше одразу піти на резервного провайдера.
LLM_TIMEOUT = 20


def _llm(model: str):
    """('провайдер:модель') → (клієнт, чиста назва моделі)."""
    prov, sep, bare = (model or "").partition(":")
    if not sep or prov not in LLM_PROVIDERS:
        prov, bare = "groq", model
    cfg = LLM_PROVIDERS[prov]
    cli = _llm_clients.get(prov)
    if cli is None:
        from openai import OpenAI
        key = _secret(cfg["key_name"]) if cfg.get("key_name") else ""
        if cfg.get("key_name") and not key:
            log.warning(f"{cfg['key_name']} не задано (secrets.json або змінна середовища): "
                        f"запити до {prov} не пройдуть, працюватиме лише резерв")
        cli = OpenAI(api_key=key or cfg.get("key_default", "none"),
                     base_url=cfg["base_url"], timeout=LLM_TIMEOUT, max_retries=0)
        _llm_clients[prov] = cli
        log.info(f"LLM-провайдер піднято: {prov}")
    return cli, bare


class LLMUnavailable(Exception):
    """Не відповіла ні основна модель, ні резервна. errors: [(модель, помилка)]."""
    def __init__(self, errors):
        super().__init__("; ".join(f"{m}: {e}" for m, e in errors))
        self.errors = errors

    @property
    def rate_limited(self) -> bool:
        return all(_is_rate_limit(e) for _, e in self.errors)


def _is_rate_limit(e) -> bool:
    s = str(e).lower()
    return "429" in s or "rate_limit" in s or "rate limit" in s or "quota" in s


def _fallback_for(model: str) -> str:
    """Резерв за замовчуванням: інша модель пари основна/резервна."""
    return GROQ_FALLBACK_MODEL if model == GROQ_PRIMARY_MODEL else GROQ_PRIMARY_MODEL


def _llm_call(model: str, messages, **kw):
    cli, bare = _llm(model)
    return cli.chat.completions.create(model=bare, messages=messages,
                                       **_model_kwargs(bare), **kw)


def llm_chat(model: str, messages, fallback=None, **kw):
    """
    Єдина точка виклику моделі. Сама підставляє параметри під провайдера.

    Якщо модель не відповіла з БУДЬ-ЯКОЇ причини (429, таймаут, 5xx, обрив
    мережі, модель прибрали), питаємо резервну на іншому провайдері.
    fallback: None → автоматично інша модель пари основна/резервна;
              False → без резерву (зір: на Groq моделей із зображеннями немає).
    Якщо не відповіла жодна, кидає LLMUnavailable.
    """
    try:
        return _llm_call(model, messages, **kw)
    except Exception as e:
        alt = _fallback_for(model) if fallback is None else fallback
        if not alt or alt == model:
            raise LLMUnavailable([(model, e)]) from e
        log.warning(f"LLM {model} не відповіла ({type(e).__name__}: {str(e)[:150]}), пробую {alt}")
        try:
            return _llm_call(alt, messages, **kw)
        except Exception as e2:
            log.error(f"LLM резерв {alt} теж не відповів: {type(e2).__name__}: {str(e2)[:150]}")
            raise LLMUnavailable([(model, e), (alt, e2)]) from e2


# Розбір тега дії. Терпимий до пробілів і регістру: gpt-oss інколи пише
# "[ ACTION:spotify_pause: ]", і сувора версія такий тег просто не бачила.
_ACTION_RE = re.compile(r"\[\s*(?:ACTION\s*:\s*)?([A-Za-z_]+)\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)

# Модель регулярно пише дію БЕЗ дужок: «brain_plan:купити молоко».
# Рішення при цьому правильне — гине лише формат. Тому ловимо й такий вигляд,
# але тільки на початку відповіді й лише для відомих префіксів дій,
# щоб звичайний текст із двокрапкою не став дією.
_BARE_ACTION_RE = re.compile(r"^\s*([a-z][a-z_]{2,24})\s*:\s*(.*)$", re.S)
_ACTION_PREFIXES = (
    "brain_", "note_", "dictate_", "spotify_", "open_", "search_", "web_",
    "clipboard_", "calendar_", "gmail_", "slack_", "system_", "monitor_",
    "screen_", "focus_", "memory_", "volume_", "window_", "notepad_",
    "claude_", "ask_", "kill_", "type_", "voice_", "timer", "weather",
    "currency", "hotkey", "shutdown", "get_time", "minimize_", "close_",
)


def _find_action(reply: str):
    """Шукає дію спершу в дужках, потім у голому вигляді. → (тип, параметр, чи_в_дужках)."""
    m = _ACTION_RE.search(reply)
    if m:
        return m.group(1).lower(), m.group(2).strip(), True
    m = _BARE_ACTION_RE.match(reply or "")
    if m:
        name = m.group(1).lower()
        if name.startswith(_ACTION_PREFIXES) or name in ("weather", "timer", "hotkey"):
            log.info(f"Дія без дужок, приймаю: {name}")
            return name, m.group(2).strip(), False
    return None, None, False


def _model_kwargs(model: str) -> dict:
    """
    Параметри під конкретну модель.

    gpt-oss і qwen3.6 — МІРКУВАЛЬНІ. Без обмеження міркувань вони витрачають
    увесь ліміт токенів на «думання» і повертають порожню відповідь або
    обрізаний тег дії. Перевірено 2026-08-15 при міграції з llama-3.3.
    """
    m = (model or "").lower()
    if m.startswith("openai/gpt-oss"):
        # ТІЛЬКИ low. На medium модель думає над кожним словом величезного
        # системного промта: у бою це дало 30 СЕКУНД на одну відповідь
        # (лог 2026-08-15, 16:47:00 → 16:47:30). У коротких тестах medium
        # виглядав нормально саме тому, що там промт був крихітний.
        return {"reasoning_effort": "low"}
    if m.startswith("qwen/"):
        return {"reasoning_effort": "none"}
    if "gemini-3.7" in m or "-pro" in m:
        # Ці беремо САМЕ заради міркувань — не глушимо їх.
        return {"reasoning_effort": "low"}
    if "gemini-3" in m or "gemini-2" in m:
        # Решта Gemini теж міркує, і без обмеження відповідь обривається
        # на півслові: «Зрозум», «`.\n\n2.». Перевірено 2026-08-15.
        return {"reasoning_effort": "none"}
    return {}


# Окрема модель для коротких команд. Порожньо = усе йде на основну.
FAST_MODEL = ""


def _choose_model(text: str) -> str:
    """
    Яку модель питати першою.

    Раніше короткі команди йшли на «швидку llama-8b», але після переходу на
    gpt-oss і Gemini там опинилась gemini-3.1-flash-lite з найменшою квотою
    (500 на день, 15 за хвилину, і її ж їсть зір). Тепер усе йде на основну
    модель, резерв лише на помилку. Щоб повернути окрему модель для коротких
    команд, задай FAST_MODEL у config.json. Теми з _FORCE_PRIMARY туди не
    потрапляють ніколи.
    """
    t = text.lower()
    if (FAST_MODEL and not any(h in t for h in _FORCE_PRIMARY)
            and len(t.split()) <= 6 and any(h in t for h in _COMMAND_HINTS)):
        return FAST_MODEL
    return GROQ_PRIMARY_MODEL


# Відповідь на підтвердження: дія лише на ЧІТКЕ «так» без «ні», мовчання = ні
_YES_WORDS = {"так", "ага", "угу", "давай", "ок", "окей", "добре", "звичайно",
              "канєшно", "конєшно", "ясно", "поїхали", "жени", "да", "yes", "go"}
_NO_WORDS  = {"ні", "нє", "нєа", "відміна", "скасуй", "стоп", "нет", "no", "не"}


def _confirm(question: str, extra_yes=()) -> bool:
    speak(question)
    ans = listen(timeout=8, phrase_limit=5) or ""
    words = set(re.sub(r"[^\w\s']", " ", ans.lower()).split())
    ok = bool(words & (_YES_WORDS | set(extra_yes))) and not (words & _NO_WORDS)
    log.info(f"Підтвердження «{question}»: '{ans}' → {'так' if ok else 'ні'}")
    return ok


# Незворотні дії → (питання, додаткові слова згоди)
_MUST_CONFIRM = {
    "system_shutdown_pc": ("Вимкнути компʼютер? Скажи так або ні.", ("вимикай",)),
    "system_restart":     ("Перезавантажити компʼютер? Скажи так або ні.", ("перезавантажуй",)),
    "kill_process":       ("Закрити «{p}»? Скажи так або ні.", ("закривай", "вбивай")),
    "note_clear":         ("Стерти всі плани? Скажи так або ні.", ("стирай", "видаляй")),
}


def ask_lin(user_input: str) -> str:
    history.append({"role": "user", "content": user_input})
    log.info(f"Запит: '{user_input}'")
    # Оновлюємо system prompt з актуальною пам'яттю і нотатками
    history[0] = {"role": "system", "content": build_system_prompt()}
    # Обмежуємо розмір контексту: system + останні 10 повідомлень (5 обмінів)
    if len(history) > 11:
        history[1:] = history[-10:]

    # Основна модель, а резервна (інший провайдер) на БУДЬ-ЯКУ помилку:
    # 429, таймаут, 5xx, обрив мережі. Раніше резерв вмикався лише на 429.
    primary = _choose_model(user_input)
    secondary = GROQ_FALLBACK_MODEL if primary == GROQ_PRIMARY_MODEL else GROQ_PRIMARY_MODEL
    log.info(f"Модель: {primary}")
    try:
        response = llm_chat(
            primary,
            messages=inject_time(history),
            fallback=secondary,
            # 180 було замало: міркувальні моделі частину ліміту витрачають на
            # думання і відповідь обривається. Сама відповідь однаково коротка:
            # модель зупиняється сама, зайвий ліміт нічого не коштує.
            max_tokens=700,
            temperature=0.7,
        )
    except LLMUnavailable as e:
        history.pop()          # запит без відповіді не лишаємо в історії
        if e.rate_limited:
            speak("Денний ліміт запитів вичерпано. Спробуй через кілька хвилин.")
        else:
            speak("Моделі зараз не відповідають. Перевір інтернет або спробуй за хвилину.")
        return ""

    # content буває None чи порожнім (міркувальна модель витратила весь ліміт
    # на думання). Такого не можна класти в історію: наступний запит з
    # assistant-повідомленням без тексту провайдер може відхилити.
    reply = (response.choices[0].message.content or "").strip()
    log.info(f"Відповідь: '{reply}'")
    if not reply:
        history.pop()
        return "Хм, загубила думку. Повтори, будь ласка."
    history.append({"role": "assistant", "content": reply})

    # Три формати: [ACTION:type:param], [type:param] і голе type:param
    action_type, action_param, _bracketed = _find_action(reply)
    if action_type:
        # Модель інколи дублює дію звичайним текстом ПЕРЕД тегом:
        # «brain_plan:подивитись мені [ACTION:brain_plan:подивитись мені]».
        # Тег вирізається дужками, а гола копія лишалась і зачитувалась уголос.
        for dup in (f"{action_type}:{action_param}", f"{action_type}:"):
            if dup and dup in reply:
                reply = reply.replace(dup, " ")
        reply = re.sub(r"\s{2,}", " ", reply).strip()

        # ── Захист від небезпечних команд ──────────────────────────────────────
        DANGEROUS = {
            "system_lock":        ["заблокуй", "lock", "блокуй", "заблок"],
            "system_sleep":       ["сплячий", "sleep", "сон", "засни", "вимкни монітор"],
            "system_restart":     ["перезавантаж", "restart", "reboot", "ребут"],
            # Без голого «вимикай»: воно є і в «вимикай музику»
            "system_shutdown_pc": ["вимкни комп", "вимикай комп", "виключи комп", "вимкни пк",
                                   "вимикай пк", "вимкни ноут", "shutdown", "вимкнення комп"],
            # close_window: ширший список — раніше блокував легітимні запити
            "close_window":       ["закрий", "закрити", "close", "зупини програму",
                                   "вийди з програми", "закрий вікно"],
            # Деструктивні / керування вводом — лише за явним наміром у запиті
            # (захист від галюцинацій моделі, помилок STT і prompt-injection).
            "kill_process":       ["вбий", "вбити", "убий", "приший", "kill",
                                   "заверши процес", "закрий процес", "вимкни процес",
                                   "зніми процес"],
            "type_text":          ["надрукуй", "напиши", "введи", "набери", "впиши",
                                   "встав", "встави", "друкуй", "type"],
            "hotkey":             ["натисни", "натисніть", "клавіш", "комбінац",
                                   "hotkey", "shortcut", "гарячу"],
            "ask_claude":         ["клод", "claude", "клода", "клоді", "клодом"],
            # Стирання нотаток. 2026-08-15: фраза «видали все, що ти написала
            # до цього в екрані» дала note_clear і знесла ВСІ плани — бо цієї
            # дії тут не було. Тепер потрібна явна згадка нотаток чи планів.
            "note_clear":         ["план", "нотатк", "записи", "список"],
            "note_delete":        ["план", "нотатк", "запис"],
            "note_done":          ["план", "нотатк", "запис", "викона", "зробив", "готово"],
        }
        if action_type in DANGEROUS:
            keywords = DANGEROUS[action_type]
            if not any(kw in user_input.lower() for kw in keywords):
                log.warning(f"Заблоковано небезпечну дію [{action_type}] — запит не містить явного наміру: '{user_input}'")
                # Текст моделі («Вимикаю…») не зачитуємо: дія ж не виконана
                return "Цього не роблю: не почула явного прохання."
        # ───────────────────────────────────────────────────────────────────────

        # ── Веб-пошук: типово відповідаємо ГОЛОСОМ; вкладку — лише на явне прохання ──
        BROWSER_WORDS = ("браузер", "браузері", "вкладк", "хром", "chrome",
                         "сайт", "сторінк", "в гуглі")
        wants_browser = any(b in user_input.lower() for b in BROWSER_WORDS)
        rerouted_to_voice = False
        if action_type == "search_web" and not wants_browser:
            action_type = "web_search"        # тиха відповідь голосом, без вкладки
            rerouted_to_voice = True
        elif action_type == "web_search" and wants_browser:
            action_type = "search_web"        # явно просять браузер — відкриваємо вкладку

        # ── Незворотні дії: питаємо ЗАВЖДИ ─────────────────────────────────────
        # Навіть коли намір у фразі звучить явно: помиляється і модель, і
        # розпізнавання, а вимкнений компʼютер з незбереженою роботою чи стерті
        # плани не повернеш. Після дії текст моделі не зачитуємо, про результат
        # скаже сама дія.
        if action_type in _MUST_CONFIRM and not (
                action_type == "note_clear" and action_param.strip().lower() in ("done", "виконані", "виконане")):
            question, extra_yes = _MUST_CONFIRM[action_type]
            if not _confirm(question.format(p=action_param[:40]), extra_yes):
                speak("Не чіпаю.")
                return ""
            log.info(f"Дія (підтверджено): [{action_type}:{action_param}]")
            execute_action(f"[ACTION:{action_type}:{action_param}]")
            return ""

        # ── Підтвердження перед відкриттям ────────────────────────────────────
        # Якщо користувач САМ явно попросив (відкрий/запусти/знайди…) — НЕ перепитуємо:
        # це зайве тертя, і якщо «так» не розпізнається, дія марно зривається.
        CONFIRM_NEEDED = {"open_app", "open_url", "search_web", "open_youtube"}
        EXPLICIT_INTENT = ("відкрий", "відкри", "відчини", "запусти", "запуст",
                           "увімкни", "ввімкни", "вмикай", "включи", "врубай",
                           "запускай", "покажи", "знайди", "пошукай", "шукай",
                           "загугли", "відкривай", "глянь")
        explicit = any(e in user_input.lower() for e in EXPLICIT_INTENT)
        if CONFIRM_ACTIONS and action_type in CONFIRM_NEEDED and not explicit:
            label = action_param or action_type
            if not _confirm(f"Відкрити «{label[:60]}»? Скажи так або ні.",
                            ("відкривай", "відкрий", "запускай")):
                log.info(f"Дію [{action_type}:{action_param}] скасовано.")
                speak("Скасовую.")
                return ""
        # ───────────────────────────────────────────────────────────────────────

        full_tag = f"[ACTION:{action_type}:{action_param}]"
        log.info(f"Дія: {full_tag}")
        _action_failed.clear()
        execute_action(full_tag)
        if _action_failed.is_set():
            return ""          # дія вже сама чесно сказала, що не вийшло
        reply = _ACTION_RE.sub("", reply).strip()
        if rerouted_to_voice:
            return ""   # відповідь озвучить сам пошук — не дублюємо ack моделі

    return reply


# ============================================================
#  SYSTEM TRAY
# ============================================================

def _star_points(cx, cy, R, r):
    """Точки 4-кутної зірки — божественна іскра в емблемі Рафаеля."""
    pts = []
    for k in range(8):
        rad = R if k % 2 == 0 else r
        a = math.pi / 2 + k * math.pi / 4
        pts.append((cx + rad * math.cos(a), cy - rad * math.sin(a)))
    return pts


def _draw_emblem(draw, S):
    """Малює емблему-німб Рафаеля на полотні ImageDraw розміром S×S (для трею/іконки)."""
    c = S / 2.0
    u = S / 64.0   # масштаб відносно базових 64px
    draw.ellipse([3*u, 3*u, S-3*u, S-3*u], fill=(18, 16, 38, 255))       # небесний диск
    for i in range(12):                                                  # промені німба
        a = i * math.pi / 6
        draw.line([c + 22*u*math.cos(a), c + 22*u*math.sin(a),
                   c + 30*u*math.cos(a), c + 30*u*math.sin(a)],
                  fill=(201, 168, 78, 255), width=max(1, int(round(2*u))))
    draw.ellipse([11*u, 11*u, S-11*u, S-11*u], outline=(233, 207, 134, 255),
                 width=max(2, int(round(3*u))))                          # золотий німб
    draw.ellipse([23*u, 23*u, S-23*u, S-23*u], fill=(20, 18, 40, 255),
                 outline=(255, 240, 192, 255), width=max(1, int(round(2*u))))  # ядро
    draw.polygon(_star_points(c, c, 9.5*u, 3.4*u), fill=(255, 246, 224, 255))  # зірка


def create_tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    _draw_emblem(ImageDraw.Draw(img), 64)
    return img


# ============================================================
#  ОСНОВНИЙ ЦИКЛ (фоновий поток)
# ============================================================

def check_mode_change(text: str) -> bool:
    """Перевіряє чи є команда зміни режиму. Повертає True якщо режим змінився."""
    global MODE
    # Прибираємо wake word щоб не заважав порівнянню
    t = vr.strip_wake_words(text.lower(), WAKE_WORDS)

    # Перехід в dictation mode
    for trigger in DICTATION_TRIGGERS:
        if trigger in t:
            MODE = "dictation"
            log.info("Режим: DICTATION")
            if LIN_UI: LIN_UI.set_mode("dictation")
            speak("Режим диктування. Говори — вставлятиму текст. Скажи 'стоп' щоб завершити.")
            return True

    # Перехід в chat mode
    for trigger in CHAT_MODE_TRIGGERS:
        if trigger in t:
            MODE = "chat"
            log.info("Режим: CHAT")
            if LIN_UI: LIN_UI.set_mode("chat")
            speak("Режим розмови активовано. Говори — я слухаю. Скажи 'стоп' або 'нормальний режим' щоб вийти.")
            return True

    # Повернення в normal mode — точне співпадіння
    for trigger in NORMAL_MODE_TRIGGERS:
        if trigger in t:
            MODE = "normal"
            log.info("Режим: NORMAL (точне)")
            if LIN_UI: LIN_UI.set_mode("normal")
            speak("Ок, виходжу.")
            return True

    # Нечітке співпадіння — тільки в chat mode, бо STT часто обрізає слова
    # ("нормальний режи", "звичайний реж", просто "стоп")
    if MODE == "chat":
        words = set(t.split())
        normal_words  = {"нормальний", "нормальн", "звичайний", "звичайн", "normal"}
        regime_words  = {"режим", "режи", "реж", "mode"}
        single_exits  = {"стоп", "досить", "все", "хватит", "хватить", "enough", "stop"}

        # Є слово типу "нормальний/звичайний" + будь-яке слово з "режим"
        if (words & normal_words) and (words & regime_words):
            MODE = "normal"
            log.info("Режим: NORMAL (нечітке: нормальний+режим)")
            speak("Ок, виходжу.")
            return True

        # Просто "стоп", "досить" тощо як окрема фраза
        if t.strip() in single_exits:
            MODE = "normal"
            log.info("Режим: NORMAL (single exit)")
            speak("Ок.")
            return True

    return False


def process_command(command: str):
    """Обробляє команду і відповідає."""
    log.info(f"Команда: '{command}'")
    print(f"\n  Ви:  {command}")

    # ── Швидкі відповіді без Groq ────────────────────────────
    cmd = command.lower().strip()

    # Повтори останню відповідь. Лише коротке «повтори / не почув / ще раз»:
    # раніше будь-яка фраза з «повтор» чи «ще раз» («повторюй трек», «вимкни
    # повтор») сюди потрапляла, і повтор у Spotify голосом був недосяжний.
    if vr.is_repeat_request(cmd):
        if _last_spoken:
            log.info("Повтор останньої відповіді")
            speak(_last_spoken)
        else:
            speak("Ще нічого не казала.")
        return

    # «Скасуй вимкнення» працює завжди і без моделі: за 30 с до вимкнення
    # чекати відповіді LLM ризиковано
    if vr.is_cancel_shutdown(cmd):
        _cancel_shutdown()
        return

    # Миттєві команди: пауза, наступний трек, гучність, час, погода. Точний
    # збіг усієї фрази, без запиту до моделі, тому спрацьовують одразу.
    instant = vr.instant_command(cmd)
    if instant:
        log.info(f"Миттєва команда: {instant}")
        if instant == "say_time":
            speak(f"Зараз {datetime.now():%H:%M}.")
        else:
            execute_action(f"[ACTION:{instant}:]")
            if LIN_UI:
                LIN_UI.safe_set_state("idle")    # пауза чи гучність мовчки: не лишати «Думаю»
        return

    # Швидкість голосу — тільки якщо це коротка команда (≤ 4 слова)
    if len(cmd.split()) <= 4:
        if any(w in cmd for w in FASTER_WORDS):
            speak(_adjust_voice_rate("faster"))
            return
        if any(w in cmd for w in SLOWER_WORDS):
            speak(_adjust_voice_rate("slower"))
            return
        if any(w in cmd for w in RESET_SPEED_WORDS):
            speak(_adjust_voice_rate("reset"))
            return

    # Очищення планів голосом. Стирання ВСІХ планів незворотне, тому так само
    # через «так/ні», як і коли це вирішує модель (раніше тут стирало одразу).
    if any(w in cmd for w in ("очисти всі плани", "видали всі плани", "очисти плани",
                              "видали всі нотатки", "очисти список планів", "видали плани")):
        question, extra_yes = _MUST_CONFIRM["note_clear"]
        speak(note_clear("all") if _confirm(question, extra_yes) else "Не чіпаю.")
        return
    if any(w in cmd for w in ("очисти виконані", "видали виконані", "прибери виконані")):
        speak(note_clear("done"))
        return

    # Push-to-talk перемикач голосом
    if any(w in cmd for w in ("режим кнопки", "тільки по кнопці", "push to talk",
                              "слухай по кнопці", "активація кнопкою")):
        _toggle_ptt(True)
        return
    if any(w in cmd for w in ("постійне слухання", "завжди слухай", "звичайне слухання",
                              "слухай завжди", "вимкни режим кнопки")):
        _toggle_ptt(False)
        return

    # Буфер — прочитати без Groq
    if any(w in cmd for w in ("що в буфері", "прочитай буфер", "покажи буфер", "clipboard")):
        speak(_clipboard_read())
        return

    # Екран — подивитись без Groq-роутингу
    SCREEN_TRIGGERS = ("що на екрані", "подивись на екран", "подивись екран",
                       "опиши екран", "що зараз на екрані", "глянь на екран",
                       "що відкрито", "дивись на екран")
    if any(w in cmd for w in SCREEN_TRIGGERS):
        threading.Thread(target=lambda: _screen_look(""), daemon=True).start()
        return

    # Здоров'я системи — без Groq-роутингу
    HEALTH_TRIGGERS = ("як справи з системою", "стан системи", "перевір систему",
                       "як система", "чи все ок з системою", "здоров'я системи",
                       "діагностика системи", "як комп")
    if any(w in cmd for w in HEALTH_TRIGGERS):
        threading.Thread(target=lambda: speak(_system_health_report()), daemon=True).start()
        return

    # Ранковий дайджест на вимогу
    if any(w in cmd for w in ("дайджест", "огляд дня", "що по дню", "брифінг",
                              "розкажи про день", "підсумок дня")):
        threading.Thread(target=lambda: morning_briefing(greeting=False), daemon=True).start()
        return

    # Календар (читання) — без Groq-роутингу
    if any(w in cmd for w in ("що в мене сьогодні", "що сьогодні", "плани на сьогодні",
                              "що в мене на сьогодні", "розклад на сьогодні")):
        threading.Thread(target=lambda: _calendar_agenda("today"), daemon=True).start()
        return
    if any(w in cmd for w in ("що в мене завтра", "що завтра", "плани на завтра",
                              "розклад на завтра")):
        threading.Thread(target=lambda: _calendar_agenda("tomorrow"), daemon=True).start()
        return
    if any(w in cmd for w in ("плани на тиждень", "що на тиждень", "розклад на тиждень")):
        threading.Thread(target=lambda: _calendar_agenda("week"), daemon=True).start()
        return

    try:
        reply = ask_lin(command)
        print(f"  Рафаель: {reply}\n")
        speak(reply)
    except Exception as e:
        log.error(f"Помилка відповіді: {e}", exc_info=True)
        speak("Вибачте, щось пішло не так.")


def _startup_greeting() -> str:
    """Вибирає привітання залежно від часу доби."""
    mem = load_memory()
    name = mem.get("user_name", "").strip()
    n = f", {name}" if name else ""

    h = datetime.now().hour
    if 5 <= h < 11:
        variants = [
            f"Доброго ранку{n}.",
            f"О, вже прокинувся{n}?",
            f"Ранок{n}. Що маємо сьогодні?",
            f"Привіт{n}, ранкова зміна.",
        ]
    elif 11 <= h < 17:
        variants = [
            f"Привіт{n}.",
            f"О, вітаю{n}.",
            f"Ну що{n}, яка задача?",
            f"Слухаю{n}.",
        ]
    elif 17 <= h < 22:
        variants = [
            f"Добрий вечір{n}.",
            f"О, вечір{n}. Що трапилось?",
            f"Вечір{n}. Я тут.",
            f"Привіт{n}, чим можу?",
        ]
    else:
        variants = [
            f"О, ще не спиш{n}?",
            f"Пізно, але я тут{n}.",
            f"Нічна зміна{n}, зрозуміло.",
            f"Слухаю{n}, хоч і пізно.",
        ]
    return random.choice(variants)


def _toggle_ptt(on: bool = None) -> None:
    """Перемикає режим push-to-talk."""
    global PUSH_TO_TALK
    PUSH_TO_TALK = (not PUSH_TO_TALK) if on is None else on
    _ptt_event.clear()
    if PUSH_TO_TALK:
        _key = TAP_TOGGLE_KEY if (TAP_TOGGLE_KEY and TAP_ACTION == "ptt") else PTT_HOTKEY
        speak(f"Режим кнопки. Слухаю після {_key.replace('+', ' ').replace('right ctrl', 'правого контролу')}.")
        log.info("PTT увімкнено")
    else:
        speak("Постійне слухання. Кажи моє ім'я як завжди.")
        log.info("PTT вимкнено")


_brain_dictating = threading.Event()   # диктовка триває
_brain_stop      = threading.Event()   # друге натискання клавіші = завершити
_brain_started_at   = 0.0              # коли почалась поточна диктовка
_brain_last_trigger = 0.0              # коли востаннє спрацювала гаряча клавіша
BRAIN_DEBOUNCE    = 0.6                # ігнорувати повторні спрацювання частіше за це
BRAIN_MIN_SESSION = 3.0                # перші секунди диктовку не зупиняємо

# Тап по одній клавіші як перемикач режиму кнопки.
# Fn НЕ ПІДХОДИТЬ: на ноутбуці її обробляє контролер клавіатури, до Windows
# не доходить жодної події (перевірено перехопленням 2026-08-15).
# Правий Ctrl доходить (scan_code 29) і окремо майже не використовується.
TAP_TOGGLE_KEY = "right ctrl"          # "" — вимкнути тап
TAP_MAX_HOLD   = 0.4                   # довше утримання = звичайний модифікатор
# Що робить тап: "ptt" — активація (надиктувати команду), "dictate" — диктовка
# у другий мозок, "toggle" — перемкнути режим кнопки.
TAP_ACTION     = "ptt"


def _dictate_to_brain() -> None:
    """
    Диктовка одразу у Вхідні другого мозку, без звертання на ім'я і без LLM.
    Що сказав — те й записалось, дослівно.

    Перше натискання починає, друге завершує. Довгі паузи між реченнями не
    обривають запис: на час диктовки поріг паузи піднімається до
    BRAIN_PAUSE_THRESHOLD, а шматки склеюються в ОДИН рядок.
    """
    global _brain_started_at, _brain_last_trigger
    now = time.monotonic()

    # Автоповтор клавіатури: утримана клавіша шле десятки натискань за секунду.
    # Без цього фільтра повтори миттєво зупиняли щойно розпочату диктовку.
    if now - _brain_last_trigger < BRAIN_DEBOUNCE:
        return
    _brain_last_trigger = now

    if _brain_dictating.is_set():
        # Захист від того самого автоповтору: перші секунди диктовку не зупиняємо.
        if now - _brain_started_at < BRAIN_MIN_SESSION:
            log.debug("Brain dictate: ранній сигнал завершити проігноровано")
            return
        _brain_stop.set()
        log.info("Brain dictate: отримано сигнал завершити")
        return

    _brain_dictating.set()
    _brain_stop.clear()
    _brain_started_at = now

    def _run():
        try:
            text = _capture_dictation("Диктуй. Скажи стоп, коли договориш.", _brain_stop)
            if not text:
                speak("Нічого не розчула.")
                return
            result = brain_capture(text)
            log.info(f"Brain dictate: {len(text)} символів")
            speak(result if len(text) < 120 else "Записала.")
        except Exception as e:
            log.error(f"Brain dictate помилка: {e}", exc_info=True)
            speak("Щось пішло не так із диктовкою.")
        finally:
            _brain_dictating.clear()
            _brain_stop.clear()
            if LIN_UI: LIN_UI.safe_set_state("idle")

    threading.Thread(target=_run, daemon=True).start()


# Прапорець тримаємо окремо від часу: час натискання може бути будь-яким
# числом, і перевіряти його на істинність не можна.
_tap_pressed = False
_tap_down_at = 0.0
_tap_combo   = False


def _tap_watcher(e) -> None:
    """
    Перемикає режим кнопки коротким тапом по одній клавіші.
    Клавіша лишається повноцінним модифікатором: якщо разом з нею натиснули
    щось іще або тримали довше за TAP_MAX_HOLD — перемикач не спрацьовує.
    """
    global _tap_pressed, _tap_down_at, _tap_combo
    try:
        if e.name == TAP_TOGGLE_KEY:
            if e.event_type == "down":
                if not _tap_pressed:            # ігноруємо автоповтор утримання
                    _tap_pressed = True
                    _tap_down_at = time.monotonic()
                    _tap_combo = False
            else:
                held = time.monotonic() - _tap_down_at if _tap_pressed else 99.0
                combo = _tap_combo
                _tap_pressed, _tap_combo = False, False
                if not combo and held < TAP_MAX_HOLD:
                    if TAP_ACTION == "toggle":
                        log.info(f"Тап по {TAP_TOGGLE_KEY}: перемикаю режим кнопки")
                        _toggle_ptt()
                    elif TAP_ACTION == "dictate":
                        log.info(f"Тап по {TAP_TOGGLE_KEY}: диктовка у мозок")
                        _dictate_to_brain()
                    else:
                        log.info(f"Тап по {TAP_TOGGLE_KEY}: активація, слухаю команду")
                        _ptt_event.set()
        elif e.event_type == "down" and _tap_pressed:
            _tap_combo = True                   # використали як модифікатор
    except Exception as ex:
        log.error(f"Тап-перемикач: {ex}")


_typing_dictation = threading.Event()
_last_dictation = ""      # остання диктовка під курсор — щоб не втратити її назавжди

# Слова, якими можна завершити диктовку голосом. Саме слово в текст не потрапляє.
_DICTATE_STOP_WORDS = ("кінець диктовки", "кінець запису", "стоп запис",
                       "кінець", "стоп", "досить", "все стоп")


def _strip_stop_word(text: str):
    """Повертає (текст без стоп-слова, чи було стоп-слово)."""
    t = text.strip()
    low = t.lower().rstrip(" .,!?")
    # Від найдовшого: інакше «все стоп» збігається з «стоп» і лишає «все».
    for w in sorted(_DICTATE_STOP_WORDS, key=len, reverse=True):
        if low.endswith(w):
            cut = len(low) - len(w)
            return t[:cut].rstrip(" .,!?—-"), True
    return t, False


def _capture_dictation(intro: str, stop_event=None) -> str:
    """
    Спільний збір диктовки: піднімає поріг паузи, накопичує фрагменти
    і завершується за стоп-словом, тишею або зовнішньою подією.
    """
    old_pause = getattr(recognizer, "pause_threshold", 0.9)
    chunks = []
    try:
        recognizer.pause_threshold = BRAIN_PAUSE_THRESHOLD
        if intro:
            speak(intro)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            part = listen(timeout=10, phrase_limit=BRAIN_DICTATE_LIMIT)
            if not part:
                break                       # тиша — вважаємо, що договорив
            cleaned, stopped = _strip_stop_word(part)
            if cleaned:
                chunks.append(cleaned)
            if stopped:
                log.info("Диктовка завершена стоп-словом")
                break
            if stop_event is not None and stop_event.is_set():
                break
    finally:
        try:
            recognizer.pause_threshold = old_pause
        except Exception:
            pass
    return " ".join(chunks).strip()


def _type_at_cursor(text: str) -> None:
    """Вставляє готовий текст туди, де курсор. Без диктовки."""
    global _last_dictation
    text = (text or "").strip()
    if not text:
        speak("А що друкувати?")
        return
    _last_dictation = text
    try:
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.08)
        try:
            if _keyboard is not None:
                _keyboard.send("ctrl+v")
            else:
                pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.hotkey("ctrl", "v")
        log.info(f"Type at cursor: {text}")
        speak("Надрукувала. Текст у буфері.")
    except Exception as e:
        log.error(f"Type at cursor: {e}")
        speak("Не вийшло надрукувати.")


def _dictate_to_cursor() -> None:
    """
    Диктовка, яка друкується туди, де стоїть курсор.
    Друкуємо вставкою з буфера, а не емуляцією клавіш: емуляція на кирилиці
    залежить від активної розкладки, вставка — ні. Попередній вміст буфера
    повертаємо назад.
    """
    if _typing_dictation.is_set():
        log.info("Dictate to cursor: вже слухаю")
        speak("Вже слухаю, диктуй.")
        return
    _typing_dictation.set()

    def _run():
        global _last_dictation
        try:
            text = _capture_dictation("Диктуй, друкую. Скажи стоп, коли договориш.")
            if not text:
                speak("Нічого не розчула.")
                return
            # Зберігаємо ДО вставки: якщо фокус не на текстовому полі, Ctrl+V
            # нікуди не потрапить, і без цієї копії текст загине безслідно.
            _last_dictation = text
            log.info(f"Dictate to cursor (текст): {text}")

            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.08)
            try:
                if _keyboard is not None:
                    _keyboard.send("ctrl+v")      # по скан-кодах, не залежить від розкладки
                else:
                    pyautogui.hotkey("ctrl", "v")
            except Exception:
                pyautogui.hotkey("ctrl", "v")

            # Старий буфер НЕ повертаємо. Раніше повертали через 0.4 с — і якщо
            # фокус був не на текстовому полі, вставка нікуди не потрапляла, а
            # відновлення буфера знищувало єдину копію надиктованого.
            # Тепер текст лишається в буфері: не вставилось — просто тисни Ctrl+V.
            speak("Надрукувала. Текст у буфері.")
        except Exception as e:
            log.error(f"Dictate to cursor помилка: {e}", exc_info=True)
            speak("Не вийшло надрукувати.")
        finally:
            _typing_dictation.clear()
            if LIN_UI: LIN_UI.safe_set_state("idle")

    threading.Thread(target=_run, daemon=True).start()


def _setup_hotkeys() -> None:
    """Реєструє глобальні гарячі клавіші (push-to-talk + диктовка у мозок)."""
    if _keyboard is None:
        log.warning("keyboard не доступний — гарячі клавіші вимкнені")
        return
    try:
        _keyboard.add_hotkey(PTT_HOTKEY, lambda: _ptt_event.set())
        _keyboard.add_hotkey(PTT_TOGGLE_HOTKEY, lambda: _toggle_ptt())
        log.info(f"Гарячі клавіші: {PTT_HOTKEY} (активація), {PTT_TOGGLE_HOTKEY} (режим)")
    except Exception as e:
        log.error(f"Не вдалося зареєструвати гарячі клавіші: {e}")

    # Диктовку реєструємо окремо: якщо саме ця комбінація зайнята іншою програмою,
    # решта гарячих клавіш має лишитись робочою.
    try:
        _keyboard.add_hotkey(BRAIN_HOTKEY, _dictate_to_brain)
        log.info(f"Гаряча клавіша диктовки у мозок: {BRAIN_HOTKEY}")
    except Exception as e:
        log.error(f"Не вдалося зареєструвати {BRAIN_HOTKEY} для диктовки: {e}")

    if TAP_TOGGLE_KEY:
        try:
            _keyboard.hook(_tap_watcher)
            what = {"toggle": "режим кнопки", "dictate": "диктовка у мозок"}.get(TAP_ACTION, "активація команди")
            log.info(f"Тап по {TAP_TOGGLE_KEY} → {what} (коротке натискання окремо)")
        except Exception as e:
            log.error(f"Не вдалося повісити тап-перемикач на {TAP_TOGGLE_KEY}: {e}")


def assistant_loop():
    global MODE
    try:
        log.info("=== Лін запущена ===")
        speak(_startup_greeting())

        while True:
            try:
                # ── Поки триває окрема сесія диктовки, основний цикл мовчить ──
                # Інакше мікрофон слухають двоє: диктовка пише текст, а цей цикл
                # той самий текст віддає моделі як команду. 2026-08-15 через це
                # надиктований абзац одночасно і надрукувався, і потрапив у мозок.
                if _typing_dictation.is_set() or _brain_dictating.is_set():
                    time.sleep(0.3)
                    continue

                # ── PUSH-TO-TALK: чекаємо натискання клавіші замість постійного слухання ──
                if PUSH_TO_TALK:
                    if not _ptt_event.wait(timeout=1.0):
                        continue          # клавішу не натиснули — чекаємо далі
                    _ptt_event.clear()
                    try:
                        import winsound
                        winsound.Beep(660, 80)   # короткий сигнал «слухаю»
                    except Exception:
                        pass
                    command = listen(timeout=8)   # одна команда без wake word
                    if command and not check_mode_change(command):
                        process_command(command)
                    continue

                # У normal-режимі це фонове чекання імені: вікно не показує
                # «Слухаю» і не виводить підслухане, а з LOCAL_WAKE_GATE фрази
                # без імені навіть не йдуть у хмару.
                waiting_name = MODE == "normal"
                text = listen(timeout=30, passive=waiting_name, wake_gate=waiting_name)
                if not text:
                    continue

                # ── DICTATION MODE: все що кажеш → вставляється в активне вікно ──
                if MODE == "dictation":
                    # Вихід лише на окреме «стоп» чи «стоп диктування» в кінці:
                    # раніше «кінець тижня» чи «стопка» теж вимикали диктування
                    if vr.is_dictation_exit(text):
                        MODE = "normal"
                        if LIN_UI: LIN_UI.set_mode("normal")
                        speak("Диктування зупинено.")
                        log.info("Режим: NORMAL (dictation exit)")
                        continue
                    # Голосова пунктуація цілими словами («команда» більше не стає «,нда»)
                    result = vr.apply_voice_punctuation(text)
                    # Вставляємо в активне вікно через буфер
                    import pyperclip as _pc
                    _saved = _pc.paste()
                    _pc.copy(result + " ")
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.25)   # чекаємо завершення вставки
                    _pc.copy(_saved)
                    log.info(f"Dictation: '{result[:60]}'")
                    if LIN_UI: LIN_UI.safe_set_state("listening", result[:50])
                    continue

                # ── CHAT MODE: слухаємо без wake word ──
                if MODE == "chat":
                    if check_mode_change(text):
                        continue
                    process_command(text)
                    continue

                # ── NORMAL MODE: чекаємо wake word ──
                # Імʼя цілим словом на початку фрази чи в кінці. Раніше шукалось
                # підрядком будь-де, і «хвилин», «лінія», «Берлін» будили Рафаеля.
                wake = vr.find_wake(text, WAKE_WORDS)
                if not wake:
                    continue
                command = wake[1]
                if LIN_UI:
                    LIN_UI.safe_set_state("thinking", command or text)

                # Перевірка зміни режиму прямо з wake word фрази
                if check_mode_change(command):
                    continue

                if not command:
                    speak("Слухаю.")
                    command = listen(timeout=8)

                if not command:
                    speak("Не почула команду.")
                    continue

                process_command(command)

            except Exception as e:
                log.error(f"Помилка в циклі: {e}", exc_info=True)

    except Exception as e:
        log.critical(f"ФАТАЛЬНА ПОМИЛКА: {e}", exc_info=True)


# ============================================================
#  MAIN
# ============================================================
#  GUI
# ============================================================

class LinUI:
    """Маленьке плаваюче вікно з анімацією стану."""

    # Палітра «Рафаель» — небесний мудрець: золото, тепле біле, блакить
    STATE_COLORS = {
        "idle":      ("#141228", "#caa84e", "✦",   "#e9cf86"),
        "listening": ("#10204a", "#7fb0ff", "🎤",  "#dcebff"),
        "thinking":  ("#1a1530", "#e9cf86", "✶",   "#fff0c0"),
        "speaking":  ("#1c1633", "#ffe9a8", "🔊",  "#fff6e0"),
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Рафаель")
        self.root.geometry("310x218+40+40")
        self.root.configure(bg="#0a0913")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)           # без рамки OS
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.9)        # напівпрозоре

        # Dragging (прив'язуємо до всіх зон вікна після їх створення)
        self._dx = self._dy = 0

        self.state  = "idle"
        self._frame = 0
        self._idle_drawn = False   # щоб не перемальовувати статичний idle-стан
        self._compact = False      # режим «лише орб» (плаваюча іконка)
        self._orb_win = None       # преміум-орб (raphael_orb.OrbWindow), лінива ініціалізація
        self._premium_active = False
        self._last_you = ""
        self._last_lin = ""

        # ── Повний інтерфейс (ховається в компактному режимі) ──
        self._full = tk.Frame(self.root, bg="#0a0913")
        self._full.pack(fill="both", expand=True)

        # ── Header ───────────────────────────────────────────
        hdr = tk.Frame(self._full, bg="#14111f", pady=5)
        hdr.pack(fill="x")
        hdr.bind("<Button-1>",  self._drag_start)
        hdr.bind("<B1-Motion>", self._drag_move)

        tk.Label(hdr, text="✦  Р А Ф А Е Л Ь", fg="#e9cf86", bg="#14111f",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=12)

        # Кнопка закрити (тільки ховає — повний вихід лише через трей)
        btn_x = tk.Label(hdr, text="✕", fg="#6a5f47", bg="#14111f",
                          font=("Segoe UI", 10), cursor="hand2", padx=8)
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda e: self.hide())
        btn_x.bind("<Enter>",    lambda e: btn_x.configure(fg="#cc6688"))
        btn_x.bind("<Leave>",    lambda e: btn_x.configure(fg="#6a5f47"))

        # Кнопка «згорнути до орба» — лишає тільки іконку Рафа
        btn_min = tk.Label(hdr, text="◯", fg="#6a5f47", bg="#14111f",
                           font=("Segoe UI", 10), cursor="hand2", padx=6)
        btn_min.pack(side="right")
        btn_min.bind("<Button-1>", lambda e: self.set_compact(True))
        btn_min.bind("<Enter>",    lambda e: btn_min.configure(fg="#e9cf86"))
        btn_min.bind("<Leave>",    lambda e: btn_min.configure(fg="#6a5f47"))

        # Кнопка "стоп TTS"
        btn_stop = tk.Label(hdr, text="⏹", fg="#6a5f47", bg="#14111f",
                             font=("Segoe UI", 10), cursor="hand2", padx=6)
        btn_stop.pack(side="right")
        btn_stop.bind("<Button-1>", lambda e: self.stop_speaking())
        btn_stop.bind("<Enter>",    lambda e: btn_stop.configure(fg="#ffaa33"))
        btn_stop.bind("<Leave>",    lambda e: btn_stop.configure(fg="#6a5f47"))

        self._mode_lbl = tk.Label(hdr, text="● normal", fg="#5f5a47",
                                   bg="#14111f", font=("Segoe UI", 7))
        self._mode_lbl.pack(side="right", padx=4)

        # ── Body ──────────────────────────────────────────────
        body = tk.Frame(self._full, bg="#0a0913")
        body.pack(fill="x", padx=10, pady=(8, 4))

        self.canvas = tk.Canvas(body, width=84, height=84, bg="#0a0913",
                                 highlightthickness=0)
        self.canvas.pack(side="left")

        txt = tk.Frame(body, bg="#0a0913")
        txt.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self._status = tk.StringVar(value="Чекаю на команду...")
        tk.Label(txt, textvariable=self._status, fg="#caa84e", bg="#0a0913",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")

        tk.Frame(txt, bg="#2a2414", height=1).pack(fill="x", pady=(3, 5))

        self._subtext = tk.StringVar(value="")
        tk.Label(txt, textvariable=self._subtext, fg="#cbb98a", bg="#0a0913",
                 font=("Segoe UI", 8), anchor="nw", wraplength=188,
                 justify="left").pack(fill="both", expand=True)

        # ── Chat history (остання репліка) ────────────────────
        tk.Frame(self._full, bg="#1a1712", height=1).pack(fill="x")
        hist = tk.Frame(self._full, bg="#07060d", pady=5)
        hist.pack(fill="x")

        self._you_var = tk.StringVar(value="")
        tk.Label(hist, textvariable=self._you_var,
                 fg="#7f93b8", bg="#07060d",
                 font=("Consolas", 7), anchor="w", padx=10,
                 wraplength=290).pack(fill="x")

        self._lin_var = tk.StringVar(value="")
        tk.Label(hist, textvariable=self._lin_var,
                 fg="#caa84e", bg="#07060d",
                 font=("Consolas", 7), anchor="w", padx=10,
                 wraplength=290).pack(fill="x")

        # ── Компактний орб (показується замість _full) ────────
        # bg = ключ прозорості (#010203): у компактному режимі робить кути вікна
        # повністю прозорими — лишається тільки круглий орб.
        self._orb = tk.Canvas(self.root, width=96, height=96, bg="#010203",
                              highlightthickness=0)
        self._orb.bind("<Button-1>",  self._drag_start)
        self._orb.bind("<B1-Motion>", self._drag_move)
        # Подвійний клік по орбу — розгорнути назад у вікно
        self._orb.bind("<Double-Button-1>", lambda e: self.set_compact(False))

        # Прив'язуємо drag до всіх ключових зон
        for w in (self.root, self._full, hdr, body, txt, hist, self.canvas):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # Подвійний клік по аватару — миттєво зупинити озвучення («тихо»)
        self.canvas.bind("<Double-Button-1>", lambda e: self.stop_speaking())

        self._animate()

    # ── Drag ─────────────────────────────────────────────────
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def hide(self):
        """Лише ховає плаваюче вікно. Повне закриття — ТІЛЬКИ через меню трею."""
        try:
            self.root.withdraw()
        except Exception:
            pass

    def set_compact(self, on: bool):
        """Перемикає повне вікно ↔ режим «лише орб» (плаваюча іконка Рафа)."""
        on = bool(on)
        if on == self._compact:
            return
        self._compact = on
        self._idle_drawn = False          # перемалювати аватар на активному полотні
        try:
            if on:
                gx, gy = self.root.winfo_x() + 100, self.root.winfo_y() + 60
                # ── Преміум-орб: окреме прозоре вікно зі сяйвом ──
                if _PREMIUM_ORB:
                    try:
                        if self._orb_win is None:
                            self._orb_win = _orb.OrbWindow(self.root, on_expand=lambda: self.set_compact(False))
                        self.root.withdraw()
                        self._orb_win.show(gx, gy, self.state)
                        self._premium_active = True
                        return
                    except Exception as e:
                        log.error(f"Преміум-орб не показався, fallback на tkinter: {e}")
                        self._premium_active = False
                        try:
                            self.root.deiconify()
                        except Exception:
                            pass
                # ── Запасний tkinter-орб (color-key прозорість) ──
                self._full.pack_forget()
                self._orb.pack(fill="both", expand=True)
                self.root.geometry("96x96")
                self.root.attributes("-transparentcolor", "#010203")
                self.root.attributes("-alpha", 1.0)
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            else:
                # ── Назад у повне вікно ──
                if self._premium_active and self._orb_win is not None:
                    self._orb_win.hide()
                    self._premium_active = False
                self._orb.pack_forget()
                self._full.pack(fill="both", expand=True)
                self.root.geometry("310x218")
                self.root.attributes("-transparentcolor", "")
                self.root.attributes("-alpha", 0.9)
                self.root.deiconify()
                self.root.attributes("-topmost", True)
        except Exception as e:
            log.debug(f"set_compact: {e}")

    def safe_toggle_compact(self):
        """Перемкнути компактний режим з будь-якого потоку (для трею)."""
        try:
            self.root.after(0, lambda: self.set_compact(not self._compact))
        except Exception:
            pass

    # ── State ─────────────────────────────────────────────────
    def set_state(self, state: str, text: str = ""):
        self.state = state
        if self._premium_active and self._orb_win is not None:
            try:
                self._orb_win.set_state(state)
            except Exception:
                pass
        labels = {
            "idle":      "Чекаю на команду...",
            "listening": "🎤  Слухаю...",
            "thinking":  "⚙  Думаю...",
            "speaking":  "🔊  Відповідаю...",
        }
        self._status.set(labels.get(state, "..."))
        if text:
            short = text[:100] + ("…" if len(text) > 100 else "")
            self._subtext.set(short)
            # оновлюємо рядок історії
            trunc = text[:50] + ("…" if len(text) > 50 else "")
            if state == "thinking":
                self._you_var.set(f"▶  {trunc}")
            elif state == "speaking":
                self._lin_var.set(f"◆  {trunc}")
        elif state == "idle":
            self._subtext.set("")

    def safe_set_state(self, state: str, text: str = ""):
        """Thread-safe оновлення стану (з будь-якого потоку)."""
        try:
            self.root.after(0, lambda: self.set_state(state, text))
        except Exception:
            pass

    def set_mode(self, mode: str):
        labels = {"chat": "💬 chat", "dictation": "🎤 dictation", "normal": "● normal"}
        label = labels.get(mode, "● normal")
        try:
            self.root.after(0, lambda: self._mode_lbl.configure(text=label))
        except Exception:
            pass

    def show(self):
        try:
            self.root.after(0, lambda: (self.root.deiconify(), self.root.lift(),
                                        self.root.attributes("-topmost", True)))
        except Exception:
            pass

    def stop_speaking(self):
        """Зупиняє TTS достроково."""
        _tts_stop.set()
        log.info("TTS зупинено кнопкою")

    # ── Animation ─────────────────────────────────────────────
    def _animate(self):
        # Якщо активний преміум-орб (окреме вікно) — tkinter-аватар не малюємо
        if self._premium_active:
            self.root.after(200, self._animate)
            return
        # Вікно сховане (✕ або трей) — малювати нікому, не палимо CPU
        try:
            hidden = self.root.state() == "withdrawn"
        except Exception:
            hidden = False
        if hidden:
            self._idle_drawn = False
            self.root.after(300, self._animate)
            return
        self._cv = self._orb if self._compact else self.canvas   # активне полотно
        cx = cy = 48 if self._compact else 42                    # центр під розмір полотна

        # Повне вікно + IDLE — статичний кадр (економія CPU). Орб завжди живий.
        if self.state == "idle" and not self._compact:
            if not self._idle_drawn:
                self._cv.delete("all")
                self._draw_idle(cx, cy)
                self._idle_drawn = True
            self.root.after(200, self._animate)
            return

        self._idle_drawn = False
        self._cv.delete("all")
        t = self._frame * 0.05
        self._frame += 1

        if self._compact:
            self._draw_orb_base(cx, cy, t)        # тіло орба + обертове «магічне кільце»

        if self.state == "idle":
            self._draw_idle(cx, cy)
        elif self.state == "listening":
            self._draw_listening(cx, cy, t)
        elif self.state == "thinking":
            self._draw_thinking(cx, cy, t)
        elif self.state == "speaking":
            self._draw_speaking(cx, cy, t)

        self.root.after(90 if (self._compact and self.state == "idle") else 50, self._animate)

    # ── Геометричні примітиви німба «Рафаеля» ────────────────
    def _star(self, cx, cy, R, r, fill, outline=""):
        """Чотирикутна зірка — божественна іскра в ядрі."""
        pts = []
        for k in range(8):
            rad = R if k % 2 == 0 else r
            a = math.pi / 2 + k * math.pi / 4
            pts += [cx + rad * math.cos(a), cy - rad * math.sin(a)]
        self._cv.create_polygon(pts, fill=fill, outline=outline, width=1)

    def _diamond(self, cx, cy, s, fill):
        self._cv.create_polygon(cx, cy - s, cx + s, cy, cx, cy + s, cx - s, cy,
                                   fill=fill, outline="")

    def _draw_orb_base(self, cx, cy, t):
        """Тіло компактного орба: темний диск + обертове «магічне кільце» + дуги-крила."""
        accent = self.STATE_COLORS.get(self.state, self.STATE_COLORS["idle"])[1]
        # Кругле тіло орба (на прозорому тлі лишається тільки воно)
        self._cv.create_oval(cx-45, cy-45, cx+45, cy+45, fill="#12101f", outline="#4a3c1d", width=1)
        self._cv.create_oval(cx-38, cy-38, cx+38, cy+38, outline="#241d10", width=1)
        # Обертове кільце з крапок (магічне коло)
        n = 18
        for i in range(n):
            a = (i / n) * 2 * math.pi + t * 0.5
            dx = cx + 42 * math.cos(a)
            dy = cy + 42 * math.sin(a)
            sz = 1.7 if i % 2 == 0 else 0.9
            self._cv.create_oval(dx-sz, dy-sz, dx+sz, dy+sz, fill=accent, outline="")
        # Дві дуги-«крила» обабіч — натяк на янгола
        self._cv.create_arc(cx-44, cy-44, cx+44, cy+44, start=55, extent=70,
                            style="arc", outline=accent, width=1)
        self._cv.create_arc(cx-44, cy-44, cx+44, cy+44, start=235, extent=70,
                            style="arc", outline=accent, width=1)

    def _draw_idle(self, cx, cy):
        # Подвійний золотий німб + ядро мудрості зі зіркою
        self._cv.create_oval(cx-32, cy-32, cx+32, cy+32, outline="#8a6f2e", width=1)
        self._cv.create_oval(cx-27, cy-27, cx+27, cy+27, outline="#caa84e", width=2)
        self._cv.create_oval(cx-13, cy-13, cx+13, cy+13,
                                 fill="#141228", outline="#e9cf86", width=2)
        self._star(cx, cy, 11, 4, "#fff0c0", "#caa84e")

    def _draw_listening(self, cx, cy, t):
        # Німб приймає сигнал — кільця пульсують (золото + блакить)
        pulse = abs(math.sin(t * 3)) * 12
        for i, (extra, col) in enumerate([(pulse, "#22407a"), (pulse*0.6, "#3a6abf"), (0, "#7fb0ff")]):
            r = 28 + extra
            self._cv.create_oval(cx-r, cy-r, cx+r, cy+r, outline=col,
                                     width=1 if i < 2 else 2)
        self._cv.create_oval(cx-13, cy-13, cx+13, cy+13,
                                 fill="#10204a", outline="#dcebff", width=2)
        self._star(cx, cy, 9, 3, "#dcebff")

    def _draw_thinking(self, cx, cy, t):
        # «Великий Мудрець аналізує» — геометричні вузли обертаються навколо ядра
        self._cv.create_oval(cx-30, cy-30, cx+30, cy+30, outline="#8a6f2e", width=1)
        shades = ["#6a5320", "#8a6f2e", "#b8902f", "#caa84e", "#e9cf86", "#fff0c0"]
        n = 6
        for i in range(n):
            a = (i / n) * 2 * math.pi + t * 3
            dx = cx + 22 * math.cos(a)
            dy = cy + 22 * math.sin(a)
            self._diamond(dx, dy, 4, shades[i % len(shades)])
        self._cv.create_oval(cx-11, cy-11, cx+11, cy+11,
                                 fill="#1a1530", outline="#e9cf86", width=2)
        self._star(cx, cy, 8, 3, "#fff0c0")

    def _draw_speaking(self, cx, cy, t):
        # Промениста пульсація — кільця розходяться від ядра
        cols = ["#fff6e0", "#ffe9a8", "#e9cf86", "#b8902f"]
        for i in range(3):
            phase = (t * 1.4 + i / 3.0) % 1.0
            r = 12 + phase * 22
            self._cv.create_oval(cx-r, cy-r, cx+r, cy+r,
                                     outline=cols[min(len(cols)-1, int(phase * len(cols)))],
                                     width=2)
        self._cv.create_oval(cx-12, cy-12, cx+12, cy+12,
                                 fill="#1c1633", outline="#ffe9a8", width=2)
        self._star(cx, cy, 10, 4, "#fff6e0", "#ffe9a8")


# ============================================================

_INSTANCE_LOCK = None   # сокет-замок (тримаємо відкритим поки Lin жива)

def _mark_stop():
    """Створює маркер свідомого виходу — щоб watchdog НЕ перезапускав Лін."""
    try:
        with open(STOP_MARKER, "w", encoding="utf-8") as f:
            f.write("stop")
    except Exception:
        pass


CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def _load_config():
    """Завантажує config.json і застосовує до глобальних налаштувань (код — запасний)."""
    if not os.path.exists(CONFIG_PATH):
        log.info("config.json не знайдено — використовую вбудовані налаштування")
        return
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log.error(f"config.json не прочитано: {e}")
        return

    g = globals()
    simple = [
        "WEATHER_CITY", "VOICE", "VOICE_RATE", "VOICE_PITCH", "CONFIRM_ACTIONS",
        "GROQ_PRIMARY_MODEL", "GROQ_FALLBACK_MODEL", "GROQ_API_KEY", "SLACK_TOKEN",
        "BRIEFING_HOUR", "PUSH_TO_TALK", "PTT_HOTKEY", "PTT_TOGGLE_HOTKEY",
        "BRAIN_HOTKEY", "BRAIN_DICTATE_LIMIT", "BRAIN_PAUSE_THRESHOLD",
        "TAP_TOGGLE_KEY", "TAP_MAX_HOLD", "TAP_ACTION",
        "SYS_MONITOR_ENABLED", "SYS_MONITOR_INTERVAL", "MONITOR_INTERVAL",
        "VISION_MODEL", "CALENDAR_TZ",
        "MAIL_TRIAGE_ENABLED", "MONITOR_GMAIL_AUTOSTART",
        "FAST_MODEL", "LOCAL_WAKE_GATE", "BRAIN_VAULT",
    ]
    applied = []
    for k in simple:
        if k in cfg:
            g[k] = cfg[k]
            applied.append(k)

    # config.json лежить у git. Ключ, покладений сюди, потрапить у репозиторій
    # з першим же комітом, тому працює, але з попередженням.
    for k in ("GROQ_API_KEY", "SLACK_TOKEN"):
        if cfg.get(k):
            log.warning(f"{k} у config.json, а цей файл у git. Перенеси ключ у secrets.json")

    if isinstance(cfg.get("SYS_THRESHOLDS"), dict):
        g["SYS_THRESHOLDS"].update(cfg["SYS_THRESHOLDS"])
        applied.append("SYS_THRESHOLDS")

    if isinstance(cfg.get("GMAIL_LABELS"), list):
        for i, lbl in enumerate(cfg["GMAIL_LABELS"]):
            if i < len(g["GMAIL_ACCOUNTS"]):
                g["GMAIL_ACCOUNTS"][i]["label"] = lbl
        applied.append("GMAIL_LABELS")

    # ── Перебудова залежних об'єктів ──
    if "GROQ_API_KEY" in cfg:
        try:
            g["client"] = _make_groq_client(g["GROQ_API_KEY"])
        except Exception as e:
            log.error(f"Groq клієнт не перестворено: {e}")
    if "VOICE_RATE" in cfg:
        try:
            g["_VOICE_RATE_VALUE"] = int(str(cfg["VOICE_RATE"]).replace("%", "").replace("+", ""))
        except Exception:
            pass
    if "PAUSE_THRESHOLD" in cfg:
        try:
            recognizer.pause_threshold = float(cfg["PAUSE_THRESHOLD"])
            applied.append("PAUSE_THRESHOLD")
        except Exception:
            pass

    log.info(f"config.json застосовано: {applied}")


def _ensure_single_instance():
    """
    Бінде сокет на 127.0.0.1:47847.
    Якщо порт вже зайнятий — інша копія Лін вже працює → виходимо.
    """
    import socket as _socket
    global _INSTANCE_LOCK
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", 47847))
        sock.listen(1)
        _INSTANCE_LOCK = sock          # тримаємо щоб не зібрав GC
        return True
    except OSError:
        sock.close()
        return False


def main():
    global LIN_UI

    # ── Завантажуємо налаштування користувача (config.json) ───
    _load_config()

    # ── Захист від подвійного запуску ─────────────────────────
    if not _ensure_single_instance():
        # Маркер ставимо ДО діалогу: інакше watchdog у start.bat сприйме наш вихід
        # як падіння і підніме копію знову — нескінченний цикл вікон.
        # start.bat сам видалить маркер у гілці :cleanstop.
        try:
            with open(STOP_MARKER, "w", encoding="utf-8") as _f:
                _f.write("duplicate instance")
        except Exception:
            pass
        import tkinter as _tk
        import tkinter.messagebox as _tkmsg
        root = _tk.Tk(); root.withdraw()
        _tkmsg.showwarning("Рафаель", "Рафаель вже запущений!\nЗакрий попередню копію через трей.")
        root.destroy()
        sys.exit(0)

    # Прибираємо маркер зупинки (ми ж щойно стартували)
    try:
        if os.path.exists(STOP_MARKER):
            os.remove(STOP_MARKER)
    except Exception:
        pass

    # ── Створюємо GUI ПЕРШИМ, щоб вітання показало анімацію ──
    LIN_UI = LinUI()
    log.info("GUI створено")

    # ── Гарячі клавіші (push-to-talk) ─────────────────────────
    _setup_hotkeys()

    # ── Запускаємо фонові потоки ──────────────────────────────
    def _mark_alive():
        try:
            with open(ALIVE_MARKER, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass
    _alive_timer = threading.Timer(ALIVE_AFTER, _mark_alive)
    _alive_timer.daemon = True
    _alive_timer.start()

    threading.Thread(target=assistant_loop,   daemon=True).start()
    threading.Thread(target=reminder_loop,    daemon=True).start()
    threading.Thread(target=briefing_loop,    daemon=True).start()
    threading.Thread(target=monitor_loop,     daemon=True).start()
    threading.Thread(target=sys_monitor_loop, daemon=True).start()

    # ── Системний трей у фоновому потоці ─────────────────────
    def _run_tray():
        def on_quit(icon, item):
            log.info("Вимкнено з треї")
            update_memory_after_session(history)
            icon.stop()
            try:
                LIN_UI.root.quit()
            except Exception:
                pass
            _mark_stop()
            os._exit(0)

        def open_log(icon, item):
            subprocess.Popen(f'notepad "{LOG_PATH}"')

        def show_window(icon, item):
            if LIN_UI:
                def _show():
                    LIN_UI.set_compact(False)        # завжди повне вікно
                    LIN_UI.root.deiconify()
                    LIN_UI.root.lift()
                    LIN_UI.root.attributes("-topmost", True)
                LIN_UI.root.after(0, _show)

        def compact_window(icon, item):
            if LIN_UI:
                LIN_UI.root.after(0, lambda: LIN_UI.set_compact(True))

        def do_digest(icon, item):
            threading.Thread(target=lambda: morning_briefing(greeting=False), daemon=True).start()

        def stop_speaking(icon, item):
            _tts_stop.set()

        def toggle_ptt_tray(icon, item):
            _toggle_ptt(not PUSH_TO_TALK)

        def toggle_sys_tray(icon, item):
            _monitor_toggle("system", not SYS_MONITOR_ENABLED)

        def toggle_mail_tray(icon, item):
            _monitor_toggle("gmail", not _MONITOR["gmail"])

        icon = pystray.Icon(
            "Рафаель",
            create_tray_icon(),
            "Рафаель — Голосовий Асистент",
            menu=pystray.Menu(
                pystray.MenuItem("✦ Рафаель активний", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("📋 Дайджест зараз", do_digest),
                pystray.MenuItem("🔇 Замовкни (стоп)", stop_speaking),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("🎤 Режим кнопки", toggle_ptt_tray,
                                 checked=lambda i: PUSH_TO_TALK),
                pystray.MenuItem("🛡️ Моніторинг системи", toggle_sys_tray,
                                 checked=lambda i: SYS_MONITOR_ENABLED),
                pystray.MenuItem("📧 Моніторинг пошти", toggle_mail_tray,
                                 checked=lambda i: _MONITOR["gmail"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Показати вікно", show_window),
                pystray.MenuItem("Згорнути до іконки", compact_window),
                pystray.MenuItem("Показати лог",   open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Вимкнути", on_quit),
            ),
        )
        log.info("Трей запущено")
        icon.run()

    threading.Thread(target=_run_tray, daemon=True).start()

    # ── Запускаємо цикл GUI на головному потоці ──────────────
    log.info("GUI запущено")
    LIN_UI.root.mainloop()

    # Якщо вікно закрили через X — зберігаємо пам'ять
    update_memory_after_session(history)
    _mark_stop()
    os._exit(0)


if __name__ == "__main__":
    main()
