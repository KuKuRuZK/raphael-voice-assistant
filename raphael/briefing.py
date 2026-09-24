"""
Ранковий дайджест: дата, плани, погода, календар, пошта, система.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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


def _part(name: str, fetch) -> str:
    """Одна частина дайджесту. Помилка в ній не має зривати решту."""
    try:
        return fetch() or ""
    except Exception as e:
        log.error(f"Дайджест {name}: {e}")
        return ""


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

    # ── 2-5. Погода, календар, пошта, система ────────────────
    # Вантажаться одночасно, поки звучить привітання. Раніше кожна частина
    # чекала попередню, і між ними була тиша на час запиту в мережу.
    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = [
            pool.submit(_part, "погода", lambda: f"Погода: {web._get_weather()}"),
            pool.submit(_part, "календар", lambda: agenda._calendar_agenda_text("today")
                        if agenda._get_calendar() else ""),
            pool.submit(_part, "пошта", lambda: gmail._gmail_unread_text(limit=5)
                        if gmail._gmail_accounts() else ""),
            pool.submit(_part, "система", sysmon._system_health_report),
        ]
        tts.speak(text)
        said = []
        for part in parts:
            said.append(part.result())
            tts.speak(said[-1])

    # Пошта й календар з мертвим токеном вище пропускаються мовчки (їх ніби
    # не налаштовано), тож причину кажемо тут, якщо її ще не прозвучало
    warning = gmail.auth_warning()
    if warning and warning not in " ".join(said):
        tts.speak(warning)

    if greeting:
        tts.speak("Гарного дня!")


def briefing_loop():
    # Дата останнього дайджесту лежить у memory.json, а не в памʼяті процесу:
    # після перезапуску в годину дайджесту (падіння, оновлення) Рафаель
    # не розповідає все вдруге
    while True:
        now = datetime.now()
        if now.hour == cfg.BRIEFING_HOUR:
            today = now.strftime("%Y-%m-%d")
            try:
                mem = notes.load_memory()
                if mem.get("last_briefing") != today:
                    mem["last_briefing"] = today
                    notes.save_memory(mem)
                    morning_briefing()
            except Exception as e:
                log.error(f"Дайджест: {e}")
        time.sleep(60)
