"""
Довіра Лін до Влада, 0..100 (memory.json, "trust"): ідея з плану енграми.

Росте від спільного часу (кожен обмін), а швидше від теплих слів; падає від
грубих і повільно, коли Влада кілька днів нема. Від рівня залежить один рядок
системного промту: наскільки вона тепла й відкрита. Характер з persona.txt
лишається, міняється лише дистанція.
"""
import logging
from datetime import datetime

from raphael import notes

log = logging.getLogger("Лін")

START = 65.0              # з чого починаємо: рівень «ви свої», тобто вона така, як у persona.txt
PER_EXCHANGE = 0.3        # кожен обмін
KIND_BONUS = 1.5          # «дякую», «молодець», «люблю»
RUDE_PENALTY = 3.0        # «дура», «заткнись»
DAILY_GAIN_CAP = 6.0      # за день не більше, щоб довіру не можна було накрутити
DECAY_PER_DAY = 2.0       # за кожен день без розмов, починаючи з другого

KIND = ("дякую", "дякс", "спасибі", "молодець", "розумниця", "умниця", "люблю", "обожнюю",
        "ти найкраща", "супер", "клас", "чудово", "дуже допомогла")
RUDE = ("дура", "тупа", "заткнись", "відвали", "ідіотка", "бісиш", "задовбала", "дебілка", "нікчема")

LEVELS = (   # (від якого рівня, як поводитись); характер той самий, міняється дистанція
    (0,  "ДОВІРА: ти на Влада ображена й відсторонена: коротко, сухо, підколки колючіші, тепла не показуєш."),
    (30, "ДОВІРА: ти трохи насторожі: менше особистого, жартуєш обережніше."),
    (55, "ДОВІРА: ви свої: звична ти, тепло й з підколками."),
    (75, "ДОВІРА: ти йому довіряєш: тепліша, інколи сама згадуєш спільне й ділишся своїми думками."),
    (90, "ДОВІРА: ви дуже близькі: відкрита, турбуєшся про нього, сама пропонуєш допомогу."),
)
BASELINE = 55             # смуга «ви свої», з якої починаємо (START)


def _trust_of(mem: dict, now: datetime) -> dict:
    t = mem.setdefault("trust", {})
    t.setdefault("value", START)
    t.setdefault("day", now.date().isoformat())
    t.setdefault("gained_today", 0.0)
    t.setdefault("last_seen", now.isoformat(timespec="minutes"))
    return t


def _decay(t: dict, now: datetime, quiet: bool = False) -> None:
    """Дні без розмов (крім першого) знімають довіру; новий день скидає денну межу росту."""
    try:
        away = (now.date() - datetime.fromisoformat(t["last_seen"]).date()).days
    except ValueError:
        away = 0
    if away >= 2:
        before = t["value"]
        t["value"] = max(0.0, t["value"] - DECAY_PER_DAY * (away - 1))
        if not quiet:
            log.info(f"Довіра: Влада не було {away} дн., {before:.0f} → {t['value']:.0f}")
    if t["day"] != now.date().isoformat():
        t["day"], t["gained_today"] = now.date().isoformat(), 0.0


def on_exchange(user_text: str, now: datetime = None) -> float:
    """Обмін відбувся: оновити довіру. → новий рівень."""
    now = now or datetime.now()
    low = (user_text or "").lower()

    def change(mem):
        t = _trust_of(mem, now)
        _decay(t, now)
        delta = PER_EXCHANGE + (KIND_BONUS if any(k in low for k in KIND) else 0.0)
        if delta > 0:
            delta = min(delta, max(0.0, DAILY_GAIN_CAP - t["gained_today"]))
            t["gained_today"] = round(t["gained_today"] + delta, 2)
        if any(r in low for r in RUDE):
            delta -= RUDE_PENALTY
        t["value"] = round(min(100.0, max(0.0, t["value"] + delta)), 2)
        t["last_seen"] = now.isoformat(timespec="minutes")
    try:
        return notes.update_memory(change)["trust"]["value"]
    except Exception as e:
        log.debug(f"Довіра: {e}")
        return START


def level(now: datetime = None) -> float:
    """Поточний рівень з урахуванням днів без розмов (без запису)."""
    now = now or datetime.now()
    t = _trust_of(notes.load_memory(), now)            # свіжа копія з файлу: запису нема
    _decay(t, now, quiet=True)                      # промт будується на кожен запит, лог не засмічуємо
    return t["value"]


def prompt_line(now: datetime = None) -> str:
    """Рядок для системного промту. На звичному рівні («ви свої») порожньо:
    persona.txt і так описує саме її, а промт іде в кожен запит."""
    value = level(now)
    start, line = [lvl for lvl in LEVELS if value >= lvl[0]][-1]
    return "" if start == BASELINE else line + " "
