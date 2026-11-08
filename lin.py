"""
Лін: точка входу. Запуск: pythonw lin.py (або start_hidden.vbs).

Тут лише те, що має статися раніше за все інше (журнал падінь, вікна дочірніх
процесів, логи), і main(): трей, фонові потоки. Уся логіка живе в
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
# перезапускав Лін кожні 5 секунд. Тепер traceback іде в crash.log.
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

from raphael import (agenda, assistant, avatar, briefing, diary, gmail, hotkeys, llm, monitor,  # noqa: E402
                     notes, runtime, stt, sysmon, tg_lin, tts, vibes)

log = logging.getLogger("Лін")

# ============================================================

_INSTANCE_LOCK = None   # сокет-замок (тримаємо відкритим поки Lin жива)


def _tray_image():
    """Значок трею: маска Лін (mask.ico у корені проєкту), без неї золоте коло."""
    from PIL import Image, ImageDraw
    try:
        return Image.open(os.path.join(cfg.SCRIPT_DIR, "mask.ico")).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([6, 6, 58, 58], fill=(20, 18, 40, 255), outline=(233, 207, 134, 255), width=5)
        return img


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


DUPLICATE_EXIT = 3          # «я друга копія»: start.bat на це не перезапускає (див. start.bat)


def main():
    # ── Завантажуємо налаштування користувача (config.json) ───
    cfg._load_config()
    stt.apply_settings()
    tts.apply_settings()

    # ── Захист від подвійного запуску ─────────────────────────
    if not _ensure_single_instance():
        # Код виходу DUPLICATE_EXIT каже start.bat «це була друга копія, не перезапускай». Раніше
        # друга копія писала .lin_stop, а він спільний: 2026-11-08 так вимкнувся перезапуск уже
        # живої Лін, і після наступного збою вона б не піднялась.
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "Лін вже запущена!\nЗакрий попередню копію через трей.",
                                         "Лін", 0x30)                     # значок «увага»
        sys.exit(DUPLICATE_EXIT)

    # Прибираємо маркер зупинки (ми ж щойно стартували)
    try:
        if os.path.exists(cfg.STOP_MARKER):
            os.remove(cfg.STOP_MARKER)
    except Exception:
        pass

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

    # Розмова, що йшла перед перезапуском, не губиться (щоденник, останні 3 години)
    assistant.history[1:] = diary.recent_turns()
    threading.Thread(target=assistant.assistant_loop,   daemon=True).start()
    threading.Thread(target=diary.diary_loop,       daemon=True).start()
    threading.Thread(target=gmail.mail_watch_loop,  daemon=True).start()   # «скажи, коли прийде лист»
    threading.Thread(target=notes.reminder_loop,    daemon=True).start()
    threading.Thread(target=briefing.briefing_loop,    daemon=True).start()
    threading.Thread(target=monitor.monitor_loop,     daemon=True).start()
    threading.Thread(target=sysmon.sys_monitor_loop, daemon=True).start()
    threading.Thread(target=agenda.calendar_reminder_loop, daemon=True).start()
    threading.Thread(target=vibes.vibe_loop,       daemon=True).start()
    threading.Thread(target=avatar.avatar_loop,    daemon=True).start()
    threading.Thread(target=tts.start_http_server, daemon=True).start()   # свій голос (TTS_HTTP_SERVER)
    threading.Thread(target=tg_lin.run, daemon=True).start()              # Лін у Telegram (є токен і id)

    # ── Системний трей у фоновому потоці ─────────────────────
    def _run_tray():
        def on_quit(icon, item):
            log.info("Вимкнено з треї")
            tg_lin.stop()
            avatar.quit_body()                   # Лін вимикається цілком: і мозок, і тіло,
            tts.stop_http_server()               # і голос (інакше лишався й тримав ≈2,5 ГБ),
            llm.local_unload_mine()              # і її модель у LM Studio
            notes.update_memory_after_session(assistant.history)
            icon.stop()
            runtime._mark_stop()
            os._exit(0)

        def open_log(icon, item):
            subprocess.Popen(f'notepad "{cfg.LOG_PATH}"')

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

        # ── Тіло: тумблери оверлея через міст (раніше в нього був свій значок) ──
        def body_toggle(key):
            return lambda icon, item: avatar.set_body(**{key: not avatar.body_state(key)})

        def body_set(**kw):
            return lambda icon, item: avatar.set_body(**kw)

        def body_do(action):
            return lambda icon, item: avatar.body(action)

        live = lambda i: avatar.online()
        BODY_ACTIONS = [("Танець", "dance"), ("Сісти на вікно", "sitA"), ("Визирнути", "peekB"), ("Музика", "music"),
                        ("Гітара", "guitar"), ("Перейти на інший монітор", "travel"), ("Телепорт", "tp"),
                        ("Стукіт", "knock"), ("Регіт", "laugh"), ("Спить", "sleep"), ("Потягується", "stretch"),
                        ("Жуйка", "bubble"), ("Істерика", "tantrum"), ("Висить", "hang"), ("Фотобомба", "photo"),
                        ("Вилазить", "climb"), ("Тицьнути", "point"), ("Голограма", "holo"), ("Краде курсор", "cursor")]
        body_menu = pystray.Menu(
            pystray.MenuItem("Автопілот", body_toggle("autopilot"), checked=lambda i: bool(avatar.body_state("autopilot")), enabled=live),
            pystray.MenuItem("Режим гри вручну", body_toggle("manual"), checked=lambda i: bool(avatar.body_state("manual")), enabled=live),
            pystray.MenuItem("Вікно мови", pystray.Menu(*[
                pystray.MenuItem(label, body_set(skin=v), checked=(lambda v: lambda i: avatar.body_state("skin") == v)(v), radio=True)
                for label, v in (("Комікс", "comic"), ("Термінал", "terminal"), ("HUD", "hud"))]), enabled=live),
            pystray.MenuItem("Аура", pystray.Menu(*[
                pystray.MenuItem(label, body_set(aura=v), checked=(lambda v: lambda i: avatar.body_state("aura") == v)(v), radio=True)
                for label, v in (("Нема", 0), ("Тихо", 1), ("Густо", 2))]), enabled=live),
            pystray.MenuItem("Дії", pystray.Menu(*[pystray.MenuItem(label, body_do(a)) for label, a in BODY_ACTIONS]), enabled=live),
            pystray.MenuItem("Повернути курсор", body_set(cursor="restore"), enabled=live),
        )

        icon = pystray.Icon(
            "Лін",
            _tray_image(),
            "Лін: голосова помічниця",
            menu=pystray.Menu(
                pystray.MenuItem("✦ Лін активна", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("📋 Дайджест зараз", do_digest),
                pystray.MenuItem("🔇 Замовкни (стоп)", stop_speaking),
                pystray.MenuItem("🖤 Тіло", body_menu),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("🎤 Режим кнопки", toggle_ptt_tray,
                                 checked=lambda i: cfg.PUSH_TO_TALK),
                pystray.MenuItem("🛡️ Моніторинг системи", toggle_sys_tray,
                                 checked=lambda i: cfg.SYS_MONITOR_ENABLED),
                pystray.MenuItem("📧 Моніторинг пошти", toggle_mail_tray,
                                 checked=lambda i: monitor._MONITOR["gmail"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Показати лог",   open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Вимкнути Лін повністю", on_quit),
            ),
        )
        log.info("Трей запущено")
        icon.run()

    # Трей і є головний цикл: окремого вікна з орбом більше нема (стан показує тіло),
    # а без tkinter процес легший. Вихід лише з трею, там os._exit.
    _run_tray()

    notes.update_memory_after_session(assistant.history)
    runtime._mark_stop()
    os._exit(0)


if __name__ == "__main__":
    main()
