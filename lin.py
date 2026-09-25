"""
Рафаель: точка входу. Запуск: pythonw lin.py (або start_hidden.vbs).

Тут лише те, що має статися раніше за все інше (журнал падінь, вікна дочірніх
процесів, логи), і main(): вікно, трей, фонові потоки. Уся логіка живе в
пакеті raphael/, карта модулів у raphael/__init__.py.
"""
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime

# ── Падіння мають лишати слід ─────────────────────────────────────────────────
# Під pythonw немає консолі (sys.stderr = None). Помилка до налаштування логів,
# наприклад не встановлена бібліотека, зникала безслідно, а start.bat тихо
# перезапускав Рафаеля кожні 5 секунд. Тепер traceback іде в crash.log.
_PYTHONW = sys.stderr is None or os.path.basename(sys.executable).lower() == "pythonw.exe"
if _PYTHONW:
    try:
        sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log"),
                          "a", encoding="utf-8", buffering=1)
        import faulthandler
        faulthandler.enable(sys.stderr)       # падіння всередині C-бібліотек (pygame, pyaudio)
    except Exception:
        pass


def _crash_hook(tp, val, tb):
    try:
        sys.stderr.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        traceback.print_exception(tp, val, tb)
    except Exception:
        pass


sys.excepthook = _crash_hook

# ── Жодних вікон консолі для дочірніх процесів ────────────────────────────────
# PowerShell/shutdown/rundll32 тощо за замовчуванням блимають чорним вікном.
# Патчимо subprocess.Popen, щоб ВСІ дочірні процеси стартували без вікна.
if os.name == "nt":
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _orig_popen_init = subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        try:
            kwargs["creationflags"] = (kwargs.get("creationflags") or 0) | _CREATE_NO_WINDOW
        except Exception:
            pass
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_init_no_window


# Налаштування першими з пакета: потрібні для шляху до лога. Решта пакета
# імпортується після налаштування логів.
from raphael import settings as cfg  # noqa: E402

# ============================================================
#  ЛОГИ
# ============================================================
def _trim_log_if_large(path: str, keep_lines: int = 1000):
    """Обрізає лог якщо він перевищив keep_lines рядків. Викликається перед init логера."""
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > keep_lines * 1.5:   # більше ніж в 1.5 рази — обрізаємо
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_lines:])
    except Exception:
        pass


_trim_log_if_large(cfg.LOG_PATH)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Файловий хендлер: INFO, ротація 500 KB, 3 архіви → максимум ~2 MB
_file_handler = logging.handlers.RotatingFileHandler(
    cfg.LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_fmt)

# Консольний хендлер: WARNING, лише коли консоль справді є. Під pythonw
# stderr веде в crash.log, і туди йдуть тільки падіння, а не кожне попередження.
_log_handlers = [_file_handler]
if not _PYTHONW:
    _con_handler = logging.StreamHandler()
    _con_handler.setLevel(logging.WARNING)
    _con_handler.setFormatter(_fmt)
    _log_handlers.append(_con_handler)

logging.basicConfig(level=logging.DEBUG, handlers=_log_handlers)

# Заглушуємо шумні бібліотеки
for _noisy in ("PIL", "httpcore", "httpx", "urllib3", "spotipy"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


import pystray  # noqa: E402

from raphael import (agenda, assistant, briefing, hotkeys, monitor,  # noqa: E402
                     notes, runtime, stt, sysmon, tts, ui)

log = logging.getLogger("Лін")

# ============================================================

_INSTANCE_LOCK = None   # сокет-замок (тримаємо відкритим поки Lin жива)


def _ensure_single_instance():
    """
    Бінде сокет на 127.0.0.1:47847.
    Якщо порт вже зайнятий — інша копія Лін вже працює → виходимо.
    """
    import socket as _socket
    global _INSTANCE_LOCK
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", 47847))
        sock.listen(1)
        _INSTANCE_LOCK = sock          # тримаємо щоб не зібрав GC
        return True
    except OSError:
        sock.close()
        return False


def main():
    # ── Завантажуємо налаштування користувача (config.json) ───
    cfg._load_config()
    stt.apply_settings()
    tts.apply_settings()

    # ── Захист від подвійного запуску ─────────────────────────
    if not _ensure_single_instance():
        # Маркер ставимо ДО діалогу: інакше watchdog у start.bat сприйме наш вихід
        # як падіння і підніме копію знову — нескінченний цикл вікон.
        # start.bat сам видалить маркер у гілці :cleanstop.
        try:
            with open(cfg.STOP_MARKER, "w", encoding="utf-8") as _f:
                _f.write("duplicate instance")
        except Exception:
            pass
        import tkinter as _tk
        import tkinter.messagebox as _tkmsg
        root = _tk.Tk(); root.withdraw()
        _tkmsg.showwarning("Рафаель", "Рафаель вже запущений!\nЗакрий попередню копію через трей.")
        root.destroy()
        sys.exit(0)

    # Прибираємо маркер зупинки (ми ж щойно стартували)
    try:
        if os.path.exists(cfg.STOP_MARKER):
            os.remove(cfg.STOP_MARKER)
    except Exception:
        pass

    # ── Створюємо GUI ПЕРШИМ, щоб вітання показало анімацію ──
    runtime.LIN_UI = ui.LinUI()
    log.info("GUI створено")

    # ── Гарячі клавіші (push-to-talk) ─────────────────────────
    hotkeys._setup_hotkeys()

    # ── Запускаємо фонові потоки ──────────────────────────────
    def _mark_alive():
        try:
            with open(cfg.ALIVE_MARKER, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass
    _alive_timer = threading.Timer(cfg.ALIVE_AFTER, _mark_alive)
    _alive_timer.daemon = True
    _alive_timer.start()

    threading.Thread(target=assistant.assistant_loop,   daemon=True).start()
    threading.Thread(target=notes.reminder_loop,    daemon=True).start()
    threading.Thread(target=briefing.briefing_loop,    daemon=True).start()
    threading.Thread(target=monitor.monitor_loop,     daemon=True).start()
    threading.Thread(target=sysmon.sys_monitor_loop, daemon=True).start()
    threading.Thread(target=agenda.calendar_reminder_loop, daemon=True).start()

    # ── Системний трей у фоновому потоці ─────────────────────
    def _run_tray():
        def on_quit(icon, item):
            log.info("Вимкнено з треї")
            notes.update_memory_after_session(assistant.history)
            icon.stop()
            try:
                runtime.LIN_UI.root.quit()
            except Exception:
                pass
            runtime._mark_stop()
            os._exit(0)

        def open_log(icon, item):
            subprocess.Popen(f'notepad "{cfg.LOG_PATH}"')

        def show_window(icon, item):
            if runtime.LIN_UI:
                def _show():
                    runtime.LIN_UI.set_compact(False)        # завжди повне вікно
                    runtime.LIN_UI.root.deiconify()
                    runtime.LIN_UI.root.lift()
                    runtime.LIN_UI.root.attributes("-topmost", True)
                runtime.LIN_UI.root.after(0, _show)

        def compact_window(icon, item):
            if runtime.LIN_UI:
                runtime.LIN_UI.root.after(0, lambda: runtime.LIN_UI.set_compact(True))

        def do_digest(icon, item):
            threading.Thread(target=lambda: briefing.morning_briefing(greeting=False), daemon=True).start()

        def stop_speaking(icon, item):
            tts._tts_stop.set()

        def toggle_ptt_tray(icon, item):
            hotkeys._toggle_ptt(not cfg.PUSH_TO_TALK)

        def toggle_sys_tray(icon, item):
            monitor._monitor_toggle("system", not cfg.SYS_MONITOR_ENABLED)

        def toggle_mail_tray(icon, item):
            monitor._monitor_toggle("gmail", not monitor._MONITOR["gmail"])

        icon = pystray.Icon(
            "Рафаель",
            ui.create_tray_icon(),
            "Рафаель — Голосовий Асистент",
            menu=pystray.Menu(
                pystray.MenuItem("✦ Рафаель активний", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("📋 Дайджест зараз", do_digest),
                pystray.MenuItem("🔇 Замовкни (стоп)", stop_speaking),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("🎤 Режим кнопки", toggle_ptt_tray,
                                 checked=lambda i: cfg.PUSH_TO_TALK),
                pystray.MenuItem("🛡️ Моніторинг системи", toggle_sys_tray,
                                 checked=lambda i: cfg.SYS_MONITOR_ENABLED),
                pystray.MenuItem("📧 Моніторинг пошти", toggle_mail_tray,
                                 checked=lambda i: monitor._MONITOR["gmail"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Показати вікно", show_window),
                pystray.MenuItem("Згорнути до іконки", compact_window),
                pystray.MenuItem("Показати лог",   open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Вимкнути", on_quit),
            ),
        )
        log.info("Трей запущено")
        icon.run()

    threading.Thread(target=_run_tray, daemon=True).start()

    # ── Запускаємо цикл GUI на головному потоці ──────────────
    log.info("GUI запущено")
    runtime.LIN_UI.root.mainloop()

    # Якщо вікно закрили через X — зберігаємо пам'ять
    notes.update_memory_after_session(assistant.history)
    runtime._mark_stop()
    os._exit(0)


if __name__ == "__main__":
    main()
