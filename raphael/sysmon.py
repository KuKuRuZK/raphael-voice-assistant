"""
Стан системи: ресурси, журнал Windows, сповіщення з кулдауном, звіт на вимогу.
"""
import json
import logging
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


def _check_resources() -> None:
    """Перевіряє CPU, RAM, диск, батарею, температуру і сповіщає при проблемах."""
    global _cpu_high_streak

    # ── CPU (стійке навантаження) ──
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        if cpu >= cfg.SYS_THRESHOLDS["cpu"]:
            _cpu_high_streak += 1
            if _cpu_high_streak >= cfg.SYS_CPU_STREAK:
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
            is_important_source = any(s in plow for s in cfg.SYS_EVENT_SOURCES)
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
