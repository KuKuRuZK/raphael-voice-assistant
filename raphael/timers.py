"""
Таймери і режим фокусу (pomodoro).
"""
import itertools
import logging
import threading
import time

from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


# ============================================================
#  РЕЖИМ ФОКУСУ / POMODORO
# ============================================================
FOCUS_ACTIVE = False


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
            tts.speak(f"Таймер завершено! Ти працював {minutes} хвилин. Зроби перерву на 5 хвилин, ти заслужив.")

    threading.Thread(target=_timer, daemon=True).start()
    return f"Режим фокусу на {minutes} хвилин. Продуктивної роботи!"


def focus_stop():
    global FOCUS_ACTIVE
    FOCUS_ACTIVE = False
    return "Режим фокусу зупинено."


# ============================================================
#  ТАЙМЕР
# ============================================================
_active_timers: dict[str, bool] = {}   # id → активний
_timer_seq = itertools.count(1)


def _timer_start(seconds: int, label: str = ""):
    """Запускає зворотний відлік. По закінченні — звук + голос."""
    if seconds <= 0:
        tts.speak("Невірний час для таймера.")
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
        tts.speak(f"{name}Таймер {time_str} — час!")
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
