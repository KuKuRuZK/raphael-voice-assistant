"""
Ранковий дайджест: дата, плани, погода, календар, пошта, система.
"""
import logging
import time
from datetime import datetime

from raphael import agenda
from raphael import gmail
from raphael import notes
from raphael import settings as cfg
from raphael import sysmon
from raphael import tts
from raphael import voice_rules as vr
from raphael import web

log = logging.getLogger("Лін")


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
    pending = [n for n in notes._load_notes() if not n["done"]]
    if pending:
        count = len(pending)
        noun  = vr.plural(count, "план", "плани", "планів")
        names = ", ".join(n["text"] for n in pending[:3])
        tail  = f" і ще {count - 3}" if count > 3 else ""
        text += f"У тебе {count} {noun}: {names}{tail}. "
    else:
        text += "Планів у нотатках немає. "
    tts.speak(text)

    # ── 2. Погода ────────────────────────────────────────────
    try:
        tts.speak(f"Погода: {web._get_weather()}")
    except Exception as e:
        log.error(f"Дайджест погода: {e}")

    # ── 3. Календар на сьогодні ──────────────────────────────
    try:
        if agenda._get_calendar():
            agenda._calendar_agenda("today")
    except Exception as e:
        log.error(f"Дайджест календар: {e}")

    # ── 4. Непрочитана пошта (обидві скриньки) ───────────────
    try:
        if gmail._gmail_accounts():
            gmail._gmail_unread(limit=5)
    except Exception as e:
        log.error(f"Дайджест пошта: {e}")

    # ── 5. Стан системи (коротко) ────────────────────────────
    try:
        tts.speak(sysmon._system_health_report())
    except Exception as e:
        log.error(f"Дайджест система: {e}")

    if greeting:
        tts.speak("Гарного дня!")


def briefing_loop():
    last_date = None
    while True:
        now = datetime.now()
        if now.hour == cfg.BRIEFING_HOUR and now.date() != last_date:
            last_date = now.date()
            morning_briefing()
        time.sleep(60)
