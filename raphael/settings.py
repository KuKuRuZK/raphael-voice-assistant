"""
Налаштування Рафаеля: значення за замовчуванням, секрети і config.json.

Інші модулі читають їх як cfg.X у момент використання, а не копіюють при
імпорті. Тому і config.json, і зміни голосом (швидкість мови, режим кнопки,
монітор системи) одразу бачать усі модулі.
"""
import json
import logging
import os

log = logging.getLogger("Лін")

# Файли користувача (secrets.json, токени, логи, нотатки, config.json) лежать
# у корені проєкту поруч із lin.py, а не в теці пакета.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
#  КОНФІГ
# ============================================================
# ── Секрети (ключі/токени) — НЕ зберігаються в коді ───────────────────────────
# Джерела за пріоритетом: 1) змінна середовища; 2) secrets.json поруч із lin.py.
# Приклад secrets.json: {"GROQ_API_KEY": "gsk_...", "SPOTIFY_CLIENT_SECRET": "..."}
# Найбезпечніше — задати змінні середовища (вони НЕ синхронізуються в OneDrive).
# LIN_SECRETS_PATH дозволяє тримати secrets.json ПОЗА OneDrive (рекомендовано).
SECRETS_PATH = os.environ.get("LIN_SECRETS_PATH") or os.path.join(
    _ROOT, "secrets.json")
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
SCRIPT_DIR = _ROOT
LOG_PATH = os.path.join(SCRIPT_DIR, "lin.log")
SCREENSHOT_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
CHROME_PATH    = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Баланс швидкість/точність розпізнавання: 0.9с тиші — Лін реагує швидше, але
# не ріже повільних мовців. Якщо обриває на півслові — підніми до 1.1; якщо
# реагує повільно — опусти до 0.7. Можна задати в config.json.
PAUSE_THRESHOLD = 0.9

# Як чекати імʼя в звичайному режимі:
#   "whisper"  кожна почута фраза йде в Groq Whisper, а імʼя шукається в тексті
#   "vosk"     імʼя шукає Vosk локально, у Whisper іде лише фраза з імʼям
#              (потрібна модель vosk-model-uk і імʼя в її словнику, інакше
#              Рафаель сам повернеться до "whisper" і напише про це в лог)
WAKE_ENGINE = "whisper"

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
GMAIL_CREDENTIALS_PATH = os.path.join(_ROOT, "gmail_credentials.json")
GMAIL_TOKEN_PATH       = os.path.join(_ROOT, "gmail_token.json")
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
    {"label": "друга",   "box": "офіційна", "token": os.path.join(_ROOT, "gmail_token2.json")},
]

# Автосортування вхідної пошти: вішати мітки Трекер/* і прибирати шум із вхідних
# на кожній перевірці монітора. Вимикається одним прапорцем.
MAIL_TRIAGE_ENABLED = True

# ── Vision (бачення екрану) ───────────────────────────────────────────────────
VISION_MODEL = "gemini:gemini-3.1-flash-lite"   # на Groq зору немає; lite бо 500/добу

# ── Моніторинг (проактивні сповіщення) ────────────────────────────────────────
MONITOR_INTERVAL = 120   # секунд між перевіркою пошти/слаку
MONITOR_GMAIL_AUTOSTART = True   # вмикати монітор пошти одразу на старті

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

# ── Push-to-talk (активація по гарячій клавіші) ───────────────────────────────
PUSH_TO_TALK      = False          # True — слухати ТІЛЬКИ після натискання клавіші
PTT_HOTKEY        = "ctrl+alt+m"   # запасна клавіша активації (основна — тап, див. TAP_*)
PTT_TOGGLE_HOTKEY = "ctrl+alt+l"   # увімкнути/вимкнути режим кнопки
BRAIN_HOTKEY      = "ctrl+alt+b"   # диктування одразу у Вхідні другого мозку
BRAIN_DICTATE_LIMIT = 60           # скільки секунд максимум триває один фрагмент диктовки
BRAIN_PAUSE_THRESHOLD = 2.5        # пауза (сек), після якої фрагмент вважається завершеним;
                                   # звичайні 0.9 рвали абзац на шматки посеред речень

# ── Watchdog (авто-перезапуск) ────────────────────────────────────────────────
STOP_MARKER = os.path.join(SCRIPT_DIR, ".lin_stop")   # створюється при свідомому виході
# Рафаель пропрацював ALIVE_AFTER секунд → старт вдався. start.bat за цим файлом
# відрізняє падіння на старті (зламана установка) від випадкового збою.
ALIVE_MARKER = os.path.join(SCRIPT_DIR, ".lin_alive")
ALIVE_AFTER  = 120

# ── Файли даних ───────────────────────────────────────────────────────────────
NOTES_PATH  = os.path.join(SCRIPT_DIR, "notes.json")    # нотатки й нагадування
MEMORY_PATH = os.path.join(SCRIPT_DIR, "memory.json")   # памʼять між сесіями
USAGE_PATH  = os.path.join(SCRIPT_DIR, "usage.json")    # лічильник денних лімітів

# ── Офлайн ────────────────────────────────────────────────────────────────────
# Без інтернету Рафаель може й далі відповідати голосом, якщо є локальні
# модель і голос. Обидва необовʼязкові: чого немає, того просто не пробуємо.
# OFFLINE_MODEL: модель Ollama, яку питати, коли не відповіли ні основна, ні
#   резервна. Напр. "local:gemma3:4b" (спершу ollama pull gemma3:4b). "" = ні.
# PIPER_EXE / PIPER_MODEL: офлайн-голос Piper, коли edge-tts недоступний.
#   piper.exe з github.com/rhasspy/piper/releases, голос
#   uk_UA-ukrainian_tts-medium.onnx разом з .onnx.json з
#   huggingface.co/rhasspy/piper-voices. Поклади все в теку piper/ поруч з lin.py.
OFFLINE_MODEL = ""
PIPER_EXE   = os.path.join(_ROOT, "piper", "piper.exe")
PIPER_MODEL = os.path.join(_ROOT, "piper", "uk_UA-ukrainian_tts-medium.onnx")

# ── Денні ліміти безкоштовного рівня (для попереджень і «скільки лімітів») ──
# Ключ «провайдер:модель». Значення з таблиць Groq і Gemini на момент запису;
# свої дивись на console.groq.com/settings/limits і в Google AI Studio, а
# змінюй у config.json (DAILY_LIMITS). Модель без ліміту тут просто рахується.
DAILY_LIMITS = {
    "groq:openai/gpt-oss-120b":     {"requests": 1000, "tokens": 200_000},
    "groq:whisper-large-v3-turbo":  {"requests": 2000, "seconds": 28_800},
    "gemini:gemini-3.1-flash-lite": {"requests": 500},
}

# ── Другий мозок (сховище Obsidian) ───────────────────────────────────────────
# Пишемо прямо у файл, а не через REST API плагіна: так працює навіть коли
# Obsidian закритий, і ключ від API не треба тримати в Ліні.
# Шлях через %USERPROFILE%, без імені користувача; інший шлях можна задати
# ключем BRAIN_VAULT у config.json
BRAIN_VAULT = os.path.expandvars(r"%USERPROFILE%\OneDrive\Документы\memory\memory")

BRIEFING_HOUR = 7                    # година ранкового дайджесту
CALENDAR_TZ   = "Europe/Vilnius"     # часовий пояс нових подій у календарі
CALENDAR_REMIND_MINUTES = 10         # за скільки хвилин нагадувати про подію (0 = ні)
CLAUDE_CMD    = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")   # Claude Code CLI

# Окрема модель для коротких команд. Порожньо = усе йде на основну.
FAST_MODEL = ""

# Тап по одній клавіші як перемикач режиму кнопки.
# Fn НЕ ПІДХОДИТЬ: на ноутбуці її обробляє контролер клавіатури, до Windows
# не доходить жодної події (перевірено перехопленням 2026-08-15).
# Правий Ctrl доходить (scan_code 29) і окремо майже не використовується.
TAP_TOGGLE_KEY = "right ctrl"          # "" — вимкнути тап
TAP_MAX_HOLD   = 0.4                   # довше утримання = звичайний модифікатор
# Що робить тап: "ptt" — активація (надиктувати команду), "dictate" — диктовка
# у другий мозок, "toggle" — перемкнути режим кнопки. Тап посеред мови
# Рафаеля завжди просто зупиняє озвучку.
TAP_ACTION     = "ptt"

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
        "VISION_MODEL", "CALENDAR_TZ", "CALENDAR_REMIND_MINUTES",
        "MAIL_TRIAGE_ENABLED", "MONITOR_GMAIL_AUTOSTART",
        "FAST_MODEL", "WAKE_ENGINE", "BRAIN_VAULT",
        "OFFLINE_MODEL", "PIPER_EXE", "PIPER_MODEL",
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

    if isinstance(cfg.get("DAILY_LIMITS"), dict):
        g["DAILY_LIMITS"].update(cfg["DAILY_LIMITS"])
        applied.append("DAILY_LIMITS")

    if isinstance(cfg.get("GMAIL_LABELS"), list):
        for i, lbl in enumerate(cfg["GMAIL_LABELS"]):
            if i < len(g["GMAIL_ACCOUNTS"]):
                g["GMAIL_ACCOUNTS"][i]["label"] = lbl
        applied.append("GMAIL_LABELS")

    if "PAUSE_THRESHOLD" in cfg:
        try:
            g["PAUSE_THRESHOLD"] = float(cfg["PAUSE_THRESHOLD"])
            applied.append("PAUSE_THRESHOLD")
        except (TypeError, ValueError):
            log.error(f"PAUSE_THRESHOLD у config.json не число: {cfg['PAUSE_THRESHOLD']!r}")
    # Клієнт Groq, швидкість голосу й поріг паузи перебудовують самі модулі:
    # main() після цього викликає stt.apply_settings() і tts.apply_settings().

    log.info(f"config.json застосовано: {applied}")
