"""
Стан системи: ресурси, журнал Windows, сповіщення з кулдауном, звіт на вимогу.
"""
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta

import psutil

from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_sys_alert_last: dict = {}     # категорія → monotonic час останнього сповіщення
_cpu_high_streak = 0           # лічильник високого CPU поспіль
_last_event_check = None       # datetime останньої перевірки журналу


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
#  МОНІТОРИНГ СИСТЕМИ
# ============================================================

def _sys_alert(category: str, message: str) -> None:
    """Озвучує системне сповіщення з кулдауном (щоб не спамило)."""
    now = time.monotonic()
    last = _sys_alert_last.get(category, 0)
    if now - last < cfg.SYS_ALERT_COOLDOWN:
        return   # ще на кулдауні
    _sys_alert_last[category] = now
    log.warning(f"SYS ALERT [{category}]: {message}")
    tts.speak(message)


# Процеси, що «займають» процесор лише формально: на Windows psutil показує
# в System Idle Process час простою
_IDLE_PROCS = {"system idle process", "idle"}


def _prime_process_cpu():
    """
    Перший замір cpu_percent для кожного процесу завжди 0%: psutil міряє
    різницю від попереднього виклику. Без цього «прогріву» за хвилину до
    сповіщення Рафаель називав винуватцем просто перший процес у списку.
    """
    for p in psutil.process_iter(["cpu_percent"]):
        pass


def _top_cpu_process() -> str | None:
    """Назва процесу, що найбільше вантажив процесор від попереднього заміру."""
    best, best_cpu = None, 0.0
    for p in psutil.process_iter(["name", "cpu_percent"]):
        name = p.info.get("name") or ""
        cpu = p.info.get("cpu_percent") or 0.0
        if name.lower() in _IDLE_PROCS or p.pid == 0:
            continue
        if cpu > best_cpu:
            best, best_cpu = name, cpu
    return best


def _check_resources() -> None:
    """Перевіряє CPU, RAM, диск, батарею, температуру і сповіщає при проблемах."""
    global _cpu_high_streak

    # ── CPU (стійке навантаження) ──
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        if cpu >= cfg.SYS_THRESHOLDS["cpu"]:
            _cpu_high_streak += 1
            if _cpu_high_streak == 1:
                _prime_process_cpu()
            if _cpu_high_streak >= cfg.SYS_CPU_STREAK:
                top = _top_cpu_process()
                who = f" Найбільше вантажить {top}." if top else ""
                _sys_alert("cpu", f"Увага: процесор завантажений на {cpu:.0f} відсотків вже довго.{who}")
        else:
            _cpu_high_streak = 0
    except Exception as e:
        log.debug(f"CPU check: {e}")

    # ── RAM ──
    try:
        mem = psutil.virtual_memory()
        if mem.percent >= cfg.SYS_THRESHOLDS["ram"]:
            _sys_alert("ram", f"Увага: пам'ять заповнена на {mem.percent:.0f} відсотків. Закрий щось важке.")
    except Exception as e:
        log.debug(f"RAM check: {e}")

    # ── Диск C: ──
    try:
        disk = psutil.disk_usage("C:\\")
        free_gb = disk.free / 1024 ** 3
        if free_gb < cfg.SYS_THRESHOLDS["disk_free_gb"]:
            _sys_alert("disk", f"Увага: на диску C залишилось лише {free_gb:.1f} гігабайт.")
    except Exception as e:
        log.debug(f"Disk check: {e}")

    # ── Батарея ──
    try:
        batt = psutil.sensors_battery()
        if batt is not None and not batt.power_plugged and batt.percent <= cfg.SYS_THRESHOLDS["battery_low"]:
            _sys_alert("battery", f"Батарея низька — {batt.percent:.0f} відсотків. Постав на зарядку.")
    except Exception as e:
        log.debug(f"Battery check: {e}")

    # ── Температура (часто недоступна на Windows) ──
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            hottest = max((t.current for sensors in temps.values() for t in sensors
                           if t.current), default=0)
            if hottest >= cfg.SYS_THRESHOLDS["temp"]:
                _sys_alert("temp", f"Увага: висока температура — {hottest:.0f} градусів.")
    except Exception as e:
        log.debug(f"Temp check: {e}")


# Журнал читає wevtutil: крихітна вбудована утиліта стартує за десятки
# мілісекунд. PowerShell для того самого запиту стартував 1-3 секунди і на
# цей час забирав ядро процесора, до того ж кожні 5 хвилин.
_LEVEL_NAMES = {1: "critical", 2: "error"}


def _query_system_log(ms: int, count: int) -> str:
    """
    XML критичних подій і помилок журналу System за останні ms мілісекунд.
    Збій самого запиту кидає OSError, а не повертає порожнечу: інакше
    зламаний запит виглядав би як «помилок у журналі немає».
    """
    query = f"*[System[(Level=1 or Level=2) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
    r = subprocess.run(
        ["wevtutil", "qe", "System", f"/q:{query}", "/f:xml", f"/c:{count}", "/rd:true"],
        capture_output=True, timeout=10,
    )
    if r.returncode != 0:
        raise OSError(f"wevtutil {r.returncode}: {_decode(r.stderr or b'').strip()[:200]}")
    return _decode(r.stdout or b"")


def _decode(raw: bytes) -> str:
    # Кодування виводу залежить від версії Windows: ловимо і UTF-16, і 8-бітне
    return raw.decode("utf-16-le" if b"\x00" in raw else "utf-8", errors="ignore")


def _parse_events(xml: str) -> list:
    """Події з виводу wevtutil /f:xml → [(provider, level, event_id)]."""
    events = []
    for ev in re.findall(r"<Event\b.*?</Event>", xml, re.S):
        provider = re.search(r"<Provider\s[^>]*?Name=['\"]([^'\"]*)", ev)
        level = re.search(r"<Level>(\d+)</Level>", ev)
        event_id = re.search(r"<EventID[^>]*>(\d+)</EventID>", ev)
        events.append((provider.group(1) if provider else "",
                       int(level.group(1)) if level else 2,
                       event_id.group(1) if event_id else ""))
    return events


def _get_event_log_errors():
    """
    Читає журнал подій Windows (System) за час від останньої перевірки.
    Повертає список (provider, level, опис) критичних/важливих помилок.
    """
    global _last_event_check
    now = datetime.now()
    since = _last_event_check or (now - timedelta(minutes=5))
    _last_event_check = now
    ms = int((now - since).total_seconds() * 1000) + 60_000   # з хвилиною запасу

    try:
        found = _parse_events(_query_system_log(ms, 25))
    except subprocess.TimeoutExpired:
        log.debug("Event log check timeout")
        return []
    except Exception as e:
        log.debug(f"Event log check: {e}")
        return []

    events = []
    for provider, level, event_id in found:
        # Критичні завжди; помилки лише з важливих джерел. Рівень числом, а не
        # назвою: назву Windows перекладає мовою системи, і «Критический»
        # раніше не збігався ні з «critical», ні з «критичний».
        if level == 1 or any(src in provider.lower() for src in cfg.SYS_EVENT_SOURCES):
            events.append((provider, _LEVEL_NAMES.get(level, "error"), f"ID {event_id}"))
    return events


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
        time.sleep(cfg.SYS_MONITOR_INTERVAL)
        if not cfg.SYS_MONITOR_ENABLED:
            continue
        try:
            _check_resources()
            cycle += 1
            if cycle >= cfg.SYS_EVENT_EVERY:
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
        cap = 20
        cnt = len(_parse_events(_query_system_log(30 * 60 * 1000, cap)))
        if cnt:
            at_least = "щонайменше " if cnt >= cap else ""
            status += (f". За останні пів години в журналі "
                       f"{at_least}{vr.count(cnt, 'помилка', 'помилки', 'помилок')}")
        else:
            status += ". Помилок у журналі немає"
    except Exception as e:
        log.debug(f"Health report events: {e}")

    return status + "."
