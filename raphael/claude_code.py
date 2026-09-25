"""
Claude Code і claude.ai: запит через CLI, читання сесій, відкриття вебверсії.
"""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import pyautogui

from raphael import pc
from raphael import settings as cfg
from raphael import tts

log = logging.getLogger("Лін")


CLAUDE_SESSIONS = os.path.join(os.path.expanduser("~"), ".claude", "projects")

CLAUDE_TIMEOUT = 120   # секунд на відповідь Claude Code


def _ask_claude_code(query: str):
    """Відправляє запит в Claude Code через CLI і зачитує відповідь вголос."""
    if not query:
        tts.speak("Що саме запитати у Клода?")
        return

    tts.speak("Запитую Клода, секунду...")

    def _run():
        try:
            result = subprocess.run(
                [cfg.CLAUDE_CMD, "-p", query, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                encoding="utf-8",
                errors="ignore",
            )
            response = (result.stdout or "").strip()
            if not response and result.stderr:
                response = result.stderr.strip()

            if response:
                log.info(f"Claude відповів ({len(response)} символів): {response[:120]}")
                # Для TTS — скорочуємо до ~400 символів, решту зберігаємо в лог
                spoken = response if len(response) <= 400 else response[:400] + "… далі дивись в логах."
                tts.speak(spoken)
            else:
                tts.speak("Клод нічого не відповів.")

        except subprocess.TimeoutExpired:
            # Раніше тут звучало «я ще чекаю, озвучу коли відповість», але
            # subprocess.run уже вбив процес, і відповіді не було б ніколи
            tts.speak(f"Клод не встиг відповісти за {CLAUDE_TIMEOUT // 60} хвилини, я зупинила запит.")
        except FileNotFoundError:
            tts.speak("Клод Код не знайдено. Перевір встановлення.")
            log.error(f"claude.cmd не знайдено: {cfg.CLAUDE_CMD}")
        except Exception as e:
            log.error(f"Claude Code помилка: {e}", exc_info=True)
            tts.speak("Щось пішло не так з Клодом.")

    threading.Thread(target=_run, daemon=True).start()


def _extract_messages_from_jsonl(path: str) -> list:
    """Витягує (role, text) пари з .jsonl файлу Claude."""
    messages = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    role = obj.get("type") or obj.get("role", "")
                    if role in ("assistant", "human", "user"):
                        content = obj.get("message", {})
                        if isinstance(content, dict):
                            text = ""
                            for block in content.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text += block.get("text", "")
                                elif isinstance(block, str):
                                    text += block
                        elif isinstance(content, str):
                            text = content
                        else:
                            text = str(content)
                        if text.strip():
                            messages.append((role, text.strip()))
                except Exception:
                    pass
    except Exception:
        pass
    return messages


def _list_claude_sessions() -> str:
    """Повертає список проектів / сесій Claude Code."""
    try:
        if not os.path.exists(CLAUDE_SESSIONS):
            return "Папка сесій Claude Code не знайдена."
        projects = []
        for entry in os.scandir(CLAUDE_SESSIONS):
            if entry.is_dir():
                # Знаходимо найновіший .jsonl у проекті
                jsonl = sorted(
                    [f.path for f in os.scandir(entry.path) if f.name.endswith(".jsonl")],
                    key=os.path.getmtime, reverse=True
                )
                if jsonl:
                    msgs = _extract_messages_from_jsonl(jsonl[0])
                    # Перший human-запит = "назва" розмови
                    first = next((t[:60] for r, t in msgs if r in ("human", "user")), entry.name[:40])
                    mtime = datetime.fromtimestamp(os.path.getmtime(jsonl[0]))
                    projects.append((mtime, first))

        if not projects:
            return "Проектів Claude Code не знайдено."

        projects.sort(reverse=True)
        lines = [f"{i+1}. {p[1]} ({p[0].strftime('%d.%m %H:%M')})"
                 for i, p in enumerate(projects[:5])]
        return "Знайдені сесії: " + "; ".join(lines)

    except Exception as e:
        log.error(f"Claude list error: {e}")
        return "Не вдалося отримати список сесій."


def _read_claude_session(n_messages: int = 5, keyword: str = "") -> str:
    """
    Читає повідомлення з Claude Code сесії.
    keyword — якщо задано, шукає файл де зустрічається це слово (пошук за темою).
    Без keyword — бере найновіший файл.
    """
    try:
        # Збираємо всі .jsonl рекурсивно
        jsonl_files = []
        for root_dir, dirs, files in os.walk(CLAUDE_SESSIONS):
            for f in files:
                if f.endswith(".jsonl"):
                    full = os.path.join(root_dir, f)
                    jsonl_files.append((os.path.getmtime(full), full))

        if not jsonl_files:
            return "Сесій Claude Code не знайдено."

        if keyword:
            kw = keyword.lower()
            # Шукаємо файл де текст містить ключове слово
            matches = []
            for mtime, path in sorted(jsonl_files, reverse=True):
                msgs = _extract_messages_from_jsonl(path)
                combined = " ".join(t.lower() for _, t in msgs)
                if kw in combined:
                    score = combined.count(kw)
                    matches.append((score, mtime, path, msgs))
            if not matches:
                return f"Чатів з темою «{keyword}» не знайдено серед {len(jsonl_files)} сесій."
            # Найрелевантніший
            matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
            messages = matches[0][3]
            log.info(f"Claude read: знайдено за '{keyword}' у {matches[0][2]}")
        else:
            _, latest = max(jsonl_files)
            messages = _extract_messages_from_jsonl(latest)

        if not messages:
            return "Повідомлень у сесії не знайдено."

        last = messages[-n_messages:]
        result_lines = []
        for role, text in last:
            label = "Ти" if role in ("human", "user") else "Клод"
            short = text[:200] + ("…" if len(text) > 200 else "")
            result_lines.append(f"{label}: {short}")
        return " / ".join(result_lines)

    except Exception as e:
        log.error(f"Claude session read error: {e}")
        return "Не вдалося прочитати сесію Claude."


def _ask_claude_web(query: str):
    """Відкриває Claude.ai і вводить запит після завантаження сторінки."""
    pc._open_url("https://claude.ai/new")
    log.info(f"Claude відкрито, запит: '{query}'")
    if not query:
        return

    def _type_after_load():
        time.sleep(4)  # чекаємо завантаження сторінки
        try:
            pyautogui.press("escape")
            time.sleep(0.3)
            # Вводимо через буфер — підтримує кирилицю
            import pyperclip as _pc
            _old = _pc.paste()
            _pc.copy(query)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            _pc.copy(_old)
            pyautogui.press("enter")
            log.info("Claude: запит введено і відправлено")
        except Exception as e:
            log.error(f"Claude type error: {e}")

    threading.Thread(target=_type_after_load, daemon=True).start()
