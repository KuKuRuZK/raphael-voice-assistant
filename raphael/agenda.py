"""
Google Календар: події на сьогодні, завтра, тиждень, створення подій і
нагадування голосом за кілька хвилин до початку.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta

from raphael import gmail
from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_calendar_service_cache = None
_calendar_creds = None   # з якими креденшелами зібрано сервіс
# Календар смикають команди, дайджест і нагадування з різних потоків, а
# httplib2 під сервісом не потокобезпечний: запити йдуть по одному
_cal_lock = threading.Lock()
_DAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]


def _get_calendar():
    """
    Повертає Google Calendar сервіс (з кешу). Коли gmail скинув мертвий токен
    і підхопив новий після gmail_auth.bat, креденшели інші, і сервіс
    збирається наново, без перезапуску.
    """
    global _calendar_service_cache, _calendar_creds
    creds = gmail._get_google_creds()
    if not creds:
        return None
    if _calendar_service_cache is not None and creds is _calendar_creds:
        return _calendar_service_cache
    try:
        from googleapiclient.discovery import build
        _calendar_service_cache = build("calendar", "v3", credentials=creds)
        _calendar_creds = creds
        log.info("Calendar сервіс ініціалізовано")
        return _calendar_service_cache
    except Exception as e:
        log.error(f"Calendar init: {e}")
        return None


def _unavailable() -> str:
    """Чому календаря немає: відвалився доступ Google чи його не налаштовано."""
    return gmail.auth_warning() or "Календар не налаштований."


def _event_start(ev: dict):
    """Початок події в місцевому часі (naive); None для подій на весь день."""
    st = ev.get("start", {}).get("dateTime")
    if not st:
        return None
    return datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _declined(ev: dict) -> bool:
    """Запрошення, від якого відмовились: ні в списку, ні в нагадуваннях."""
    return any(a.get("self") and a.get("responseStatus") == "declined"
               for a in ev.get("attendees", []))


def _calendar_agenda_text(which: str = "today") -> str:
    """Події текстом: today / tomorrow / week (для озвучки і дайджесту)."""
    svc = _get_calendar()
    if not svc:
        return _unavailable()
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

        with _cal_lock:
            res = svc.events().list(
                calendarId="primary",
                timeMin=start.astimezone().isoformat(),
                timeMax=end.astimezone().isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=15,
            ).execute()
        items = [ev for ev in res.get("items", []) if not _declined(ev)]
        if not items:
            return f"{label} подій немає."
        lines = []
        for ev in items[:6]:
            summary = ev.get("summary", "без назви")
            # Час у твоєму поясі, а не в поясі того, хто створив подію; на
            # тиждень ще й день, інакше «10:00 зустріч» незрозуміло коли
            dt = _event_start(ev)
            day = ""
            if which == "week":
                d = dt.date() if dt else date.fromisoformat(ev.get("start", {}).get("date", "")[:10])
                day = _DAYS[d.weekday()] + " "
            lines.append(f"{day}{dt:%H:%M} {summary}" if dt else f"{day}{summary}")
        log.info(f"Calendar {which}: {len(items)} подій")
        return f"{label} у тебе {vr.count(len(items), 'подія', 'події', 'подій')}: " + "; ".join(lines) + "."
    except Exception as e:
        if gmail._is_auth_error(e):
            gmail._auth_problem(cfg.GMAIL_TOKEN_PATH, e)
            return _unavailable()
        log.error(f"Calendar agenda: {e}")
        return "Не вдалося прочитати календар."


def _calendar_agenda(which: str = "today") -> None:
    """Озвучує події: today / tomorrow / week."""
    tts.speak(_calendar_agenda_text(which))


def _calendar_create(summary: str, start_str: str, minutes: int = 60) -> None:
    """Створює подію. start_str у форматі 'YYYY-MM-DD HH:MM'."""
    svc = _get_calendar()
    if not svc:
        tts.speak(_unavailable())
        return
    try:
        start_dt = datetime.strptime(start_str.strip(), "%Y-%m-%d %H:%M")
        end_dt   = start_dt + timedelta(minutes=minutes)
        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": cfg.CALENDAR_TZ},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": cfg.CALENDAR_TZ},
        }
        with _cal_lock:
            svc.events().insert(calendarId="primary", body=body).execute()
        _reminders["stale"] = True       # нова подія може початись за 10 хвилин
        when = start_dt.strftime("%d.%m о %H:%M")
        tts.speak(f"Додала в календар: «{summary}» на {when}.")
        log.info(f"Calendar create: {summary} @ {start_str}")
    except ValueError:
        tts.speak("Не зрозуміла дату чи час події.")
    except Exception as e:
        if gmail._is_auth_error(e):
            gmail._auth_problem(cfg.GMAIL_TOKEN_PATH, e)
            tts.speak("Подію не створила. " + _unavailable())
            return
        log.error(f"Calendar create: {e}")
        tts.speak("Не вдалося створити подію.")


# ── Нагадування про події ─────────────────────────────────────────────────────
CAL_REFRESH = 300                  # секунд між запитами подій до Google
_reminders = {"stale": True}       # stale: перечитати події на наступному колі


def _upcoming_events(hours: int = 2):
    """
    Події з часом на найближчі години: [(id, початок, назва)].
    None, якщо календар зараз недоступний (тоді лишаються старі).
    """
    svc = _get_calendar()
    if not svc:
        return None
    now = datetime.now()
    try:
        with _cal_lock:
            res = svc.events().list(
                calendarId="primary",
                timeMin=now.astimezone().isoformat(),
                timeMax=(now + timedelta(hours=hours)).astimezone().isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=20,
            ).execute()
    except Exception as e:
        if gmail._is_auth_error(e):
            gmail._auth_problem(cfg.GMAIL_TOKEN_PATH, e)
        else:
            log.error(f"Calendar reminders: {e}")
        return None
    out = []
    for ev in res.get("items", []):
        start = _event_start(ev)
        if start and not _declined(ev):
            out.append((ev.get("id", ""), start, ev.get("summary", "без назви")))
    return out


def _due_reminders(events: list, now: datetime, announced: set, minutes: int) -> list:
    """Тексти нагадувань про події, що почнуться протягом minutes хвилин."""
    out = []
    for ev_id, start, summary in events:
        left = (start - now).total_seconds() / 60
        if 0 < left <= minutes and (ev_id, start) not in announced:
            announced.add((ev_id, start))
            m = max(1, round(left))
            out.append(f"Нагадую: через {vr.count(m, 'хвилину', 'хвилини', 'хвилин')} «{summary}».")
    return out


def calendar_reminder_loop():
    """Фоновий потік: за CALENDAR_REMIND_MINUTES до події каже про неї голосом."""
    log.info(f"Нагадування календаря: за {cfg.CALENDAR_REMIND_MINUTES} хв")
    events, fetched_at, announced = [], 0.0, set()
    while True:
        time.sleep(30)
        if not cfg.CALENDAR_REMIND_MINUTES:
            continue
        try:
            if _reminders["stale"] or time.monotonic() - fetched_at >= CAL_REFRESH:
                _reminders["stale"] = False
                fetched_at = time.monotonic()
                fresh = _upcoming_events()
                if fresh is not None:
                    events = fresh
                else:
                    # Без календаря нагадування мовчки зникли б; якщо причина
                    # в доступі Google, кажемо про це (раз на день, як монітор)
                    warning = gmail.auth_warning(daily=True)
                    if warning:
                        tts.speak(warning)
            now = datetime.now()
            for text in _due_reminders(events, now, announced, cfg.CALENDAR_REMIND_MINUTES):
                log.info(text)
                tts.speak(text)
            announced = {k for k in announced if k[1] > now}
        except Exception as e:
            log.error(f"Calendar reminders: {e}")
