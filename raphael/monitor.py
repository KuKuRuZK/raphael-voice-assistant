"""
Фоновий монітор пошти й Slack: оголошує нове, що варте голосу.
"""
import logging
import time

from raphael import gmail
from raphael import mail_triage
from raphael import settings as cfg
from raphael import slack_chat
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_MONITOR = {"gmail": False, "slack": False}   # які монітори увімкнені
_monitor_primed = {"gmail": False, "slack": False}   # перший прохід — без спаму


def _monitor_toggle(target: str, on: bool) -> None:
    """Вмикає/вимикає монітор пошти, слаку або системи."""
    target = target.lower().strip()

    # Система — окремий прапорець
    if target in ("system", "система", "систему", "all", "все", "всі"):
        cfg.SYS_MONITOR_ENABLED = on
        if target in ("system", "система", "систему"):
            tts.speak("Стежу за системою." if on else "Більше не стежу за системою.")
            return

    targets = ["gmail", "slack"] if target in ("all", "все", "всі") else [target]

    done = []
    for tg in targets:
        if tg not in _MONITOR:
            continue
        if on:
            # Перевіряємо що сервіс взагалі налаштований
            if tg == "gmail" and not gmail._gmail_accounts():
                tts.speak(gmail.auth_warning() or "Спершу налаштуй Gmail, потрібен файл credentials.")
                continue
            if tg == "slack" and not slack_chat._get_slack():
                tts.speak("Спершу налаштуй Slack — потрібен токен у конфігу.")
                continue
            _MONITOR[tg] = True
            _monitor_primed[tg] = False   # перший прохід запам'ятає поточний стан без спаму
        else:
            _MONITOR[tg] = False
        done.append(tg)

    if not done:
        return
    names = {"gmail": "пошта", "slack": "Slack"}
    label = " і ".join(names.get(d, d) for d in done)
    if on:
        tts.speak(f"Стежу за {label}. Сповіщу коли щось нове.")
    else:
        tts.speak(f"Більше не стежу за {label}.")


def monitor_loop():
    """Фоновий потік — періодично перевіряє пошту і Slack, оголошує нове."""
    # Монітор пошти вмикається сам, інакше автосортування не працює доти,
    # доки про нього не згадаєш і не тицьнеш у трей після кожного перезапуску.
    if cfg.MONITOR_GMAIL_AUTOSTART:
        _MONITOR["gmail"] = True
    log.info(f"Monitor loop запущено (пошта: {'увімкнена' if _MONITOR['gmail'] else 'вимкнена'}, "
             f"сортування: {'так' if cfg.MAIL_TRIAGE_ENABLED and mail_triage else 'ні'})")
    while True:
        time.sleep(cfg.MONITOR_INTERVAL)
        try:
            # ── Gmail ──
            if _MONITOR["gmail"]:
                new_mail = gmail._gmail_check_new()
                warning = gmail.auth_warning(daily=True)
                if warning:
                    tts.speak(warning)
                first_pass = not _monitor_primed["gmail"]
                _monitor_primed["gmail"] = True
                if first_pass and not (cfg.MAIL_TRIAGE_ENABLED and mail_triage):
                    # Без міток памʼяті між запусками немає: перший прохід лише
                    # запамʼятовує, щоб не зачитати всі старі непрочитані.
                    # З мітками нерозкладене на старті справді нове (прийшло,
                    # поки компʼютер був вимкнений), і про нього варто сказати.
                    pass
                elif new_mail:
                    if len(new_mail) == 1:
                        _, sender, subj = new_mail[0]
                        tts.speak(f"Новий лист від {sender}: «{subj}».")
                    else:
                        senders = ", ".join(s for _, s, _ in new_mail[:3])
                        tts.speak(f"{vr.count(len(new_mail), 'новий лист', 'нові листи', 'нових листів')}, "
                              f"зокрема від {senders}.")

            # ── Slack ──
            if _MONITOR["slack"]:
                new_msgs = slack_chat._slack_check_new()
                if not _monitor_primed["slack"]:
                    _monitor_primed["slack"] = True
                elif new_msgs:
                    if len(new_msgs) == 1:
                        uname, text = new_msgs[0]
                        tts.speak(f"Slack — {uname} згадав тебе: {text}.")
                    else:
                        unames = ", ".join(u for u, _ in new_msgs[:3])
                        tts.speak(f"{len(new_msgs)} нових згадок у Slack від {unames}.")
        except Exception as e:
            log.error(f"Monitor loop помилка: {e}")
