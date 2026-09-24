"""
Розбір голосових фраз для Рафаеля: без мікрофона, мережі й Windows.

Сюди винесено саме ті місця, де раніше жили баги з порівнянням «підрядком»:
ім'я ловилось усередині слова («хвилин» будило через «лин»), «повтор» з'їдав
команди Spotify, «код» відкривав VS Code замість Claude Code. Усе тут чисті
функції, тому перевіряється тестами (tests/test_voice_rules.py).
"""
import re
from datetime import datetime, timedelta
from functools import lru_cache

# Літери слова, включно з апострофами, які дає розпізнавання
_W = r"[\w'ʼ’]"


@lru_cache(maxsize=32)
def _words_re(words: frozenset):
    """Регулярка, що ловить будь-яке зі слів ЦІЛИМ словом (довші першими)."""
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!{_W})(?:{alts})(?!{_W})", re.IGNORECASE)


def normalize(text: str) -> str:
    """Нижній регістр, без розділових знаків і зайвих пробілів."""
    t = re.sub(r"[^\w\s'ʼ’-]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip(" -")


# ── Ім'я (wake word) ──────────────────────────────────────────────────────────
WAKE_MAX_POSITION = 3   # ім'я має бути серед перших трьох слів фрази


def find_wake(text: str, wake_words) -> tuple | None:
    """
    Шукає звертання до асистента. → (ім'я, команда без імені) або None.

    Ім'я рахується, лише якщо це ціле слово на початку фрази («Рафа, увімкни
    музику», «слухай Рафа, ...») або останнє слово («увімкни музику, Рафа»).
    Раніше ім'я шукалось як підрядок будь-де, і будили його «хвилин», «лінія»,
    «Берлін», «фотографа», у тому числі власні фрази («Таймер 5 хвилин»).
    """
    text = text or ""
    for m in _words_re(frozenset(wake_words)).finditer(text):
        before = re.findall(rf"{_W}+", text[:m.start()])
        after = re.findall(rf"{_W}+", text[m.end():])
        if len(before) < WAKE_MAX_POSITION or not after:
            command = f"{text[:m.start()]} {text[m.end():]}"
            command = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", command))
            return m.group(0).lower(), command.strip(" ,.!?;:-")
    return None


def strip_wake_words(text: str, wake_words) -> str:
    """Прибирає всі звертання цілими словами (для перевірки зміни режиму)."""
    t = _words_re(frozenset(wake_words)).sub(" ", text or "")
    return re.sub(r"\s+", " ", t).strip(" ,.!?")


# ── «Повтори» ─────────────────────────────────────────────────────────────────
REPEAT_PHRASES = (
    "повтори", "повторити", "ще раз", "скажи ще раз", "повтори ще раз",
    "не чув", "не почув", "не почула", "не розчув", "не розчула",
    "що ти сказала", "що ти сказав", "що сказала", "repeat",
)
# Слова, з якими це вже не «повтори останню фразу», а команда
# («повторюй трек», «спробуй ще раз увімкнути музику»)
_NOT_REPEAT = re.compile(
    r"трек|пісн|плейлист|музик|альбом|spotify|спотіф|спотиф|шафл"
    r"|спробуй|увімкн|ввімкн|постав|відкрий|зроби|запусти",
    re.IGNORECASE)


def is_repeat_request(text: str) -> bool:
    """Коротке «повтори / не почув / ще раз», а не команда зі словом «повтор»."""
    t = normalize(text)
    if not t or len(t.split()) > 5 or _NOT_REPEAT.search(t):
        return False
    return bool(_words_re(frozenset(REPEAT_PHRASES)).search(t))


# ── Миттєві команди (без LLM) ─────────────────────────────────────────────────
# Лише ТОЧНИЙ збіг усієї фрази: пошук підрядком тут уже раз перехоплював
# чужі команди. Все, що не збіглось дослівно, іде в модель, як і раніше.
INSTANT_COMMANDS = {
    "spotify_pause":  ("пауза", "на паузу", "постав на паузу", "постав паузу",
                       "зупини музику", "стоп музика", "вимкни музику", "тихо музика"),
    "spotify_resume": ("продовж", "продовжуй", "грай далі", "віднови музику",
                       "зніми з паузи", "прибери паузу"),
    "spotify_next":   ("наступна", "наступний", "наступний трек", "наступна пісня",
                       "наступну", "наступну пісню", "перемкни трек", "скіп"),
    "spotify_prev":   ("попередня", "попередній", "попередній трек",
                       "попередня пісня", "попередню", "попередню пісню"),
    "spotify_like":   ("лайк", "лайкни", "лайкни трек", "лайкни цей трек", "постав лайк",
                       "додай в улюблені", "додай трек в улюблені", "в улюблені"),
    "spotify_unlike": ("прибери лайк", "зніми лайк", "прибери з улюблених", "видали з улюблених"),
    "volume_up":      ("гучніше", "голосніше", "зроби гучніше", "зроби голосніше"),
    "volume_down":    ("тихіше", "тихше", "зроби тихіше", "зроби тихше"),
    "say_time":       ("котра година", "яка година", "скільки зараз часу",
                       "котра зараз година", "скажи котра година"),
    "weather":        ("яка погода", "погода", "що з погодою", "яка зараз погода",
                       "яка погода сьогодні", "погода сьогодні"),
    "usage_report":   ("ліміти", "які ліміти", "покажи ліміти", "скільки лімітів",
                       "скільки лімітів лишилось", "скільки лімітів залишилось",
                       "скільки запитів", "скільки запитів лишилось"),
}
_INSTANT_LOOKUP = {phrase: action for action, phrases in INSTANT_COMMANDS.items()
                   for phrase in phrases}
_POLITE = re.compile(r"\b(будь ласка|будь-ласка|плз|пліз)\b")


def instant_command(text: str) -> str | None:
    """Назва дії, якщо фраза дослівно одна з миттєвих команд, інакше None."""
    t = normalize(_POLITE.sub(" ", (text or "").lower()))
    return _INSTANT_LOOKUP.get(t)


# ── Скасування вимкнення ──────────────────────────────────────────────────────
_CANCEL_SHUTDOWN = re.compile(
    r"(скасуй|відміни|зупини|стоп|не)\s+(вимкнення|перезавантаження|вимикай"
    r"|перезавантажуй|вимикати|перезавантажувати)", re.IGNORECASE)


def is_cancel_shutdown(text: str) -> bool:
    return bool(_CANCEL_SHUTDOWN.search(normalize(text)))


# ── Режим диктування ──────────────────────────────────────────────────────────
DICTATION_EXIT_PHRASES = ("стоп", "зупини", "кінець", "досить", "стоп диктування",
                          "вийди з диктування", "завершити диктування",
                          "заверши диктування", "stop dictation")
_EXIT_TAIL = re.compile(r"(стоп диктування|вийди з диктування|заверш\w* диктування"
                        r"|stop dictation)$")


def is_dictation_exit(text: str) -> bool:
    """Вихід лише на окрему фразу («стоп») чи явне «стоп диктування» в кінці.
    Раніше «кінець тижня» чи «стопка» теж вимикали диктування."""
    t = normalize(text)
    return t in DICTATION_EXIT_PHRASES or bool(_EXIT_TAIL.search(t))


# Голосова пунктуація. Ціле слово, інакше «команда» ставала «,нда».
DICTATION_PUNCT = {
    "крапка з комою": ";", "знак питання": "?", "знак оклику": "!",
    "відкрити дужку": "(", "закрити дужку": ")", "новий рядок": "\n",
    "абзац": "\n\n", "двокрапка": ":", "крапка": ".", "кома": ",", "тире": " — ",
}
_ATTACH_LEFT = {";", "?", "!", ")", ":", ".", ","}   # ці знаки липнуть до слова зліва


def apply_voice_punctuation(text: str) -> str:
    """«привіт кома як справи знак питання» → «привіт, як справи?»"""
    out = text or ""
    for word in sorted(DICTATION_PUNCT, key=len, reverse=True):   # «крапка з комою» раніше за «крапку»
        sym = DICTATION_PUNCT[word]
        w = rf"(?<!{_W}){re.escape(word)}(?!{_W})"
        if sym in _ATTACH_LEFT:
            out = re.sub(rf"\s*{w}", sym, out, flags=re.IGNORECASE)
        elif sym == "(":
            out = re.sub(rf"{w}\s*", " (", out, flags=re.IGNORECASE)
        elif not sym.strip():                                      # новий рядок, абзац
            out = re.sub(rf"[ \t]*{w}[ \t]*", sym, out, flags=re.IGNORECASE)
        else:                                                      # тире
            out = re.sub(rf"\s*{w}\s*", sym, out, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", out).strip(" ")


# ── Програми ──────────────────────────────────────────────────────────────────
def match_app_key(name: str, keys) -> str | None:
    """
    Ключ програми, що стоїть у назві ЦІЛИМ словом. Довші ключі перевіряються
    першими: інакше «клод код» ловився ключем «код» (VS Code), а «claude code»
    ключем «claude» (сайт).
    """
    n = (name or "").lower().strip()
    for key in sorted(keys, key=len, reverse=True):
        if re.search(rf"(?<!{_W}){re.escape(key)}(?!{_W})", n):
            return key
    return None


# ── Відмінки ──────────────────────────────────────────────────────────────────
def plural(n, one: str, few: str, many: str) -> str:
    """Українська множина: 1 лист, 2 листи, 5 листів, 11 листів, 21 лист."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def count(n, one: str, few: str, many: str) -> str:
    """«3 листи», «1 подія»."""
    return f"{n} {plural(n, one, few, many)}"


# ── Час нагадування ───────────────────────────────────────────────────────────
def parse_remind_time(value: str, now: datetime | None = None) -> str | None:
    """
    Час нагадування → "YYYY-MM-DD HH:MM" або None, якщо не зрозуміло.
      "21:00", "9:30", "21.00"   сьогодні о вказаний час (завтра, якщо вже минув)
      "2026-09-26 09:00"         конкретна дата (так модель передає «завтра о 9»,
                                 «у пʼятницю о 10»: дату вона бачить у промпті)
      "30"                       через 30 хвилин
    Неможливий час («25:00») і дата в минулому дають None, а не виняток.
    """
    value = (value or "").strip()
    now = now or datetime.now()
    if not value or value == "0":
        return None

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2})[:.](\d{2})", value)
    if m:
        try:
            target = datetime.strptime(f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}",
                                       "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return target.strftime("%Y-%m-%d %H:%M") if target > now else None

    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if h > 23 or mn > 59:
            return None
        target = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.strftime("%Y-%m-%d %H:%M")

    if value.isdigit() and int(value) > 0:
        return (now + timedelta(minutes=int(value))).strftime("%Y-%m-%d %H:%M")
    return None


def describe_remind(remind_at: str, now: datetime | None = None) -> str:
    """"2026-09-25 09:00" → «завтра о 09:00», «о 21:00», «26.09 о 10:00»."""
    now = now or datetime.now()
    try:
        dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return f"о {str(remind_at)[11:]}"
    days = (dt.date() - now.date()).days
    if days == 0:
        return f"о {dt:%H:%M}"
    if days == 1:
        return f"завтра о {dt:%H:%M}"
    return f"{dt:%d.%m} о {dt:%H:%M}"
