"""
Google Календар: події на сьогодні, завтра, тиждень і створення подій.
"""
import logging
from datetime import datetime, timedelta

from raphael import gmail
from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_calendar_service_cache = None
_calendar_creds = None   # з якими креденшелами зібрано сервіс


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

        res = svc.events().list(
            calendarId="primary",
            timeMin=start.astimezone().isoformat(),
            timeMax=end.astimezone().isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=15,
        ).execute()
        items = res.get("items", [])
        if not items:
            return f"{label} подій немає."
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
        svc.events().insert(calendarId="primary", body=body).execute()
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
