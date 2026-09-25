"""
Керування компʼютером: програми, вікна, процеси, гучність, буфер обміну,
скріншоти, погляд на екран, вимкнення з можливістю скасувати.
"""
import difflib
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import psutil
import pyautogui

from raphael import llm
from raphael import notes
from raphael import runtime
from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")

# Рафаель сам натискає клавіші; кут екрана не має зупиняти pyautogui
pyautogui.FAILSAFE = False


# ============================================================
#  ФУНКЦІЇ КЕРУВАННЯ ПК
# ============================================================

# Шляхи через змінні середовища (%APPDATA%, %LOCALAPPDATA%), а не з іменем
# користувача: так працює на будь-якому компʼютері і не світить імʼя в репо.
_P = os.path.expandvars

APP_MAP = {
    # Браузери
    "браузер":      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "хром":         r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "chrome":       r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge":         "msedge",
    "microsoft edge": "msedge",

    # Месенджери
    "spotify":      _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "спотіфай":     _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "спотифай":     _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "музика":       _P(r"%APPDATA%\Spotify\Spotify.exe"),
    "discord":      _P(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"),
    "дискорд":      _P(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"),
    "telegram":     _P(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
    "телеграм":     _P(r"%APPDATA%\Telegram Desktop\Telegram.exe"),

    # Microsoft Office
    "ворд":         r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "word":         r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":        r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "ексель":       r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint":   r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    "презентація":  r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",

    # Розробка
    "код":          _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "vscode":       _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "visual studio": _P(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "android studio": r"C:\Program Files\Android\Android Studio\bin\studio64.exe",
    "xampp":        r"C:\xampp\xampp-control.exe",

    # 3D / Creative
    "blender":      r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
    "unity":        r"C:\Program Files\Unity Hub\Unity Hub.exe",
    "audacity":     r"C:\Program Files\Audacity\Audacity.exe",

    # Ігри
    "steam":        r"C:\Program Files (x86)\Steam\Steam.exe",
    "dota":         r"C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\bin\win64\dota2.exe",
    "elden ring":   r"C:\Program Files (x86)\Steam\steamapps\common\ELDEN RING\Game\eldenring.exe",
    "wallpaper":    r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper32.exe",

    # Системні
    "блокнот":      "notepad",
    "notepad":      "notepad",
    "нотатник":     "notepad",
    "текстовий":    "notepad",
    "нотатки":      f'notepad "{cfg.NOTES_PATH}"',
    "мої нотатки":  f'notepad "{cfg.NOTES_PATH}"',
    "плани":        f'notepad "{cfg.NOTES_PATH}"',
    "калькулятор":  "calc",
    "calculator":   "calc",
    "провідник":    "explorer",
    "файли":        "explorer",
    "paint":        "mspaint",
    "малювання":    "mspaint",
    "7zip":         r"C:\Program Files\7-Zip\7zFM.exe",
    "архіватор":    r"C:\Program Files\7-Zip\7zFM.exe",
    "razer":        r"C:\Program Files (x86)\Razer\Razer Cortex\RazerCortex.exe",

    # AI / Веб-сервіси
    "claude":       "https://claude.ai/new",
    "клод":         "https://claude.ai/new",
    "claude ai":    "https://claude.ai/new",
    "claude code":  f'start cmd /k "{cfg.CLAUDE_CMD}"',
    "клод код":     f'start cmd /k "{cfg.CLAUDE_CMD}"',
    "chatgpt":      "https://chatgpt.com",
    "чатгпт":       "https://chatgpt.com",
    "gemini":       "https://gemini.google.com",
    "джемін":       "https://gemini.google.com",
    "github":       "https://github.com",
    "гітхаб":       "https://github.com",
}


def open_application(name: str):
    n = name.lower().strip()

    # 1. Точний пошук: ключ цілим словом, довші ключі першими. Раніше ключі
    # перевірялись підрядком у порядку словника, і «клод код» відкривав
    # VS Code (ключ «код»), а «claude code» сайт (ключ «claude»).
    key = vr.match_app_key(n, APP_MAP)
    if key:
        _launch(key, APP_MAP[key])
        return

    # 2. Fuzzy matching — ловить "спотіфай", "блендер", "дискорд" тощо
    best = difflib.get_close_matches(n, APP_MAP.keys(), n=1, cutoff=0.55)
    if best:
        key = best[0]
        log.info(f"Fuzzy match: '{n}' → '{key}'")
        _launch(key, APP_MAP[key])
        return

    # 3. Пошук по словах запиту
    words = n.split()
    for word in words:
        matches = difflib.get_close_matches(word, APP_MAP.keys(), n=1, cutoff=0.7)
        if matches:
            key = matches[0]
            log.info(f"Word fuzzy: '{word}' → '{key}'")
            _launch(key, APP_MAP[key])
            return

    log.warning(f"Програму не знайдено: {name}")
    tts.speak(f"Не знайшла програму «{name}».")
    runtime._mark_action_failed()


def _open_url(url: str):
    """Chrome, якщо він стоїть там, де очікуємо, інакше браузер за замовчуванням."""
    if os.path.exists(cfg.CHROME_PATH):
        subprocess.Popen([cfg.CHROME_PATH, url])
    else:
        os.startfile(url)


def _launch(key: str, path: str):
    try:
        if path.startswith("http"):
            _open_url(path)
        elif os.path.exists(path):
            # Реальний файл (часто .exe зі ПРОБІЛАМИ у шляху) — через ShellExecute,
            # БЕЗ shell: інакше cmd ламає шлях на пробілі ("C:\Program" + "Files...").
            os.startfile(path)
        else:
            # Прості команди (notepad, calc) і shell-конструкції (start cmd /k "...")
            subprocess.Popen(path, shell=True)
        log.info(f"Відкрито: {key} → {path[:60]}")
    except Exception as e:
        log.warning(f"Помилка відкриття {key}: {e}")


def take_screenshot() -> str | None:
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(cfg.SCREENSHOT_DIR, f"lin_{ts}.png")
        pyautogui.screenshot().save(path)
        log.info(f"Скріншот: {path}")
        return path
    except Exception as e:
        log.error(f"Скріншот помилка: {e}")
        return None


def volume_control(direction: str):
    key_map = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}
    steps = 1 if direction == "mute" else 5
    for _ in range(steps):
        pyautogui.hotkey(key_map.get(direction, "volumemute"))
    log.info(f"Гучність: {direction}")


# Системні процеси, які голосом не закриваємо ніколи
_PROTECTED_PROCS = {
    "explorer", "svchost", "csrss", "winlogon", "wininit", "lsass", "services",
    "system", "smss", "dwm", "fontdrvhost", "sihost", "taskhostw", "ctfmon",
    "runtimebroker", "audiodg", "conhost", "searchhost", "startmenuexperiencehost",
}


def kill_process(name: str) -> int:
    """
    Закриває процеси за назвою. Спершу точний збіг («chrome» = chrome.exe),
    якщо такого немає, то за початком назви.

    Раніше збіг шукався підрядком будь-де, тому порожня назва від моделі
    («kill_process:») закрила б УСІ процеси користувача, а «e» половину.
    """
    target = (name or "").lower().strip()
    if target.endswith(".exe"):
        target = target[:-4]
    if len(target) < 3:
        log.warning(f"kill_process: назва '{name}' надто коротка, нічого не чіпаю")
        return 0
    me = os.getpid()
    exact, prefix = [], []
    for proc in psutil.process_iter(["name", "pid"]):
        pname = (proc.info.get("name") or "").lower()
        base = pname[:-4] if pname.endswith(".exe") else pname
        if not base or proc.info.get("pid") == me or base in _PROTECTED_PROCS:
            continue
        if base == target:
            exact.append(proc)
        elif base.startswith(target):
            prefix.append(proc)
    killed = 0
    for proc in exact or prefix:
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    log.info(f"Завершено процесів '{name}': {killed}")
    return killed


def _notepad_write(text: str):
    """Відкриває Блокнот і вписує текст."""
    if not text:
        tts.speak("Що саме написати в Блокноті?")
        return

    def _run():
        try:
            import pygetwindow as gw

            # Перевіряємо чи Блокнот вже відкритий
            wins = [w for w in gw.getAllWindows()
                    if "notepad" in w.title.lower() or "блокнот" in w.title.lower()
                    or "безіменний" in w.title.lower() or "untitled" in w.title.lower()]

            if not wins:
                subprocess.Popen("notepad.exe")
                time.sleep(1.2)   # чекаємо завантаження
                wins = [w for w in gw.getAllWindows()
                        if "notepad" in w.title.lower() or "безіменний" in w.title.lower()
                        or "untitled" in w.title.lower()]

            if wins:
                w = wins[0]
                w.restore()
                w.activate()
                time.sleep(0.3)
            else:
                log.warning("Блокнот не знайдено після запуску")

            # Вписуємо текст через буфер обміну (підтримує кирилицю)
            import pyperclip
            _saved_clip = pyperclip.paste()   # зберігаємо поточний буфер
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyperclip.copy(_saved_clip)        # відновлюємо
            log.info(f"Блокнот: написано '{text[:60]}'")
            tts.speak("Написала.")
        except Exception as e:
            log.error(f"notepad_write помилка: {e}", exc_info=True)
            tts.speak("Не вдалося написати в Блокноті.")

    threading.Thread(target=_run, daemon=True).start()


# ============================================================
#  БУФЕР ОБМІНУ
# ============================================================

def _clipboard_read() -> str:
    """Зачитує вголос вміст буфера обміну."""
    try:
        import pyperclip
        text = pyperclip.paste().strip()
        if not text:
            return "Буфер порожній."
        short = text[:300] + ("…" if len(text) > 300 else "")
        return f"В буфері: {short}"
    except Exception as e:
        log.error(f"Clipboard read: {e}")
        return "Не вдалося прочитати буфер."


def _clipboard_save() -> str:
    """Зберігає вміст буфера як нотатку."""
    try:
        import pyperclip
        text = pyperclip.paste().strip()
        if not text:
            return "Буфер порожній — нічого зберігати."
        short = text[:200]
        return notes.note_add(short)
    except Exception as e:
        log.error(f"Clipboard save: {e}")
        return "Не вдалося зберегти буфер."


def _clipboard_set(text: str) -> str:
    """Копіює текст в буфер обміну."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return f"Скопіювала: «{text[:60]}»."
    except Exception as e:
        log.error(f"Clipboard set: {e}")
        return "Не вдалося скопіювати."


# ============================================================
#  VISION — БАЧЕННЯ ЕКРАНУ
# ============================================================

def _capture_screen_b64(max_side: int = 1280, quality: int = 70) -> str:
    """Знімає екран, стискає і повертає base64 JPEG."""
    import base64, io
    img = pyautogui.screenshot()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _screen_look(question: str = "") -> None:
    """
    Дивиться на поточний екран через Groq vision-модель і озвучує.
    question — якщо задано, відповідає на конкретне питання про екран.
    """
    if not cfg.VISION_MODEL:
        # Groq прибрав llama-4-scout, іншої моделі із зображеннями там більше
        # немає (перевірено списком моделей 2026-08-15). Краще чесно сказати,
        # ніж падати з 404 на кожен запит про екран.
        log.warning("Screen look: vision-модель не налаштована")
        tts.speak("Зараз не бачу екран: у Groq більше немає моделі із зображеннями.")
        return
    try:
        b64 = _capture_screen_b64()
        prompt = question.strip() or "Що зараз на екрані? Опиши коротко українською, одне-два речення."
        prompt += " Відповідай ТІЛЬКИ українською, без англійської."
        resp = llm.llm_chat(
            cfg.VISION_MODEL,
            fallback=False,          # резервна gpt-oss на Groq картинок не бачить
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}],
            max_tokens=200,
            temperature=0.3,
        )
        answer = resp.choices[0].message.content.strip()
        log.info(f"Screen look: '{answer[:100]}'")
        tts.speak(answer)
    except Exception as e:
        log.error(f"Screen look: {e}", exc_info=True)
        tts.speak("Не вдалося подивитись на екран.")


SHUTDOWN_DELAY = 30   # секунд до вимкнення/ребуту: час передумати


def _cancel_shutdown():
    """shutdown /a і чесна відповідь, чи було що скасовувати."""
    try:
        # Без text=True: вивід не потрібен, а консоль пише в cp866, і
        # декодування в кодуванні системи могло впасти посеред успіху
        r = subprocess.run(["shutdown", "/a"], capture_output=True, timeout=10)
        tts.speak("Скасувала вимкнення." if r.returncode == 0 else
              "Вимкнення не було заплановане.")
    except Exception as e:
        log.error(f"shutdown /a: {e}")
        tts.speak("Не вийшло скасувати. Набери shutdown /a вручну.")
