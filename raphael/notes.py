"""
Нотатки з нагадуваннями (notes.json) і памʼять між сесіями (memory.json).
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from raphael import llm
from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_notes_cache: list = []
_notes_cache_time: float = 0.0
_NOTES_TTL = 5.0   # секунд між повторними читаннями файлу


def _load_notes() -> list:
    global _notes_cache, _notes_cache_time
    now = time.monotonic()
    if now - _notes_cache_time < _NOTES_TTL and _notes_cache is not None:
        return _notes_cache
    if os.path.exists(cfg.NOTES_PATH):
        with open(cfg.NOTES_PATH, encoding="utf-8") as f:
            _notes_cache = json.load(f)
    else:
        _notes_cache = []
    _notes_cache_time = now
    return _notes_cache


def _write_json_atomic(path: str, data):
    """Запис через тимчасовий файл: якщо процес впаде посеред запису (а watchdog
    його одразу підніме), на диску лишиться стара версія, а не обрізаний JSON."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _save_notes(notes: list):
    global _notes_cache, _notes_cache_time
    _write_json_atomic(cfg.NOTES_PATH, notes)
    _notes_cache = notes          # одразу оновлюємо кеш
    _notes_cache_time = time.monotonic()


# Розбір часу нагадування живе в voice_rules (там тести): "21:00", "30",
# "2026-09-26 09:00". Неможливий час чи минула дата дають None, а не виняток.
parse_remind_time = vr.parse_remind_time


def note_add(text: str, remind_minutes: int = 0, remind_at_str: str = "") -> str:
    """Додає нотатку/план. remind_minutes=0 — без нагадування."""
    notes = _load_notes()
    new_id = max((n["id"] for n in notes), default=0) + 1
    remind_at = None
    if remind_at_str:
        remind_at = parse_remind_time(remind_at_str)
        if remind_at is None:
            # Раніше тут мовчки записувалась звичайна нотатка, а Лін казала
            # «Записала», і ти думав, що нагадування буде. Тепер чесно.
            log.warning(f"Нагадування: не зрозуміла час '{remind_at_str}' для «{text}»")
            return (f"Не зрозуміла, коли нагадати про «{text}». Скажи, наприклад, "
                    "о 21:00, через 30 хвилин або завтра о 9.")
    elif remind_minutes > 0:
        remind_at = (datetime.now() + timedelta(minutes=remind_minutes)).strftime("%Y-%m-%d %H:%M")
    note = {
        "id": new_id,
        "text": text,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "remind_at": remind_at,
        "done": False,
    }
    notes.append(note)
    _save_notes(notes)
    log.info(f"Нотатка додана #{new_id}: {text}" + (f" (нагадування: {remind_at})" if remind_at else ""))
    if remind_at:
        return f"Записала: «{text}». Нагадаю {vr.describe_remind(remind_at)}."
    return f"Записала: «{text}»."


# ── Відкладені дії: later:час|дія:параметр ───────────────────────────────────
# «Перевір пошту через 30 хвилин» раніше ставало нагадуванням «Перевірити Gmail»:
# о 13:37 Лін лише казала це вголос (лог 2026-11-06). Тепер у нагадуванні лежить сама
# дія, і в час Х Лін її виконує. Наперед ставимо лише те, що щось читає чи вмикає:
# перевірка явного наміру в assistant бачить тільки зовнішній тег later, тож
# незворотне (вимкнути компʼютер, закрити процес, стерти плани) сюди не пускаємо.
LATER_OK = ("gmail_unread", "gmail_latest", "gmail_search", "gmail_read_full", "calendar_today",
            "calendar_tomorrow", "calendar_week", "slack_unread", "slack_dm", "slack_mentions",
            "spotify_", "weather", "get_time", "system_info", "system_health", "note_list",
            "web_search", "brain_ask", "diary_find", "mail_watch", "usage_report")
_LATER_WHAT = {
    "gmail_unread": "перевіряю пошту", "gmail_latest": "дивлюсь останні листи",
    "gmail_search": "шукаю в пошті", "gmail_read_full": "читаю лист",
    "calendar_today": "дивлюсь календар на сьогодні", "calendar_tomorrow": "дивлюсь календар на завтра",
    "calendar_week": "дивлюсь календар на тиждень", "slack_unread": "дивлюсь Slack",
    "weather": "дивлюсь погоду", "get_time": "кажу час", "system_info": "дивлюсь систему",
    "system_health": "перевіряю систему", "web_search": "шукаю в інтернеті", "mail_watch": "чекаю лист",
}


def _later_text(action: str) -> str:
    name, _, param = action.partition(":")
    what = _LATER_WHAT.get(name) or ("вмикаю музику" if name.startswith("spotify_") else name)
    return f"{what} «{param.strip()}»" if param.strip() and name in ("gmail_search", "web_search", "mail_watch") else what


def later(param: str) -> str:
    """later:30|gmail_unread: → за 30 хвилин виконати gmail_unread. Час як у note_remind."""
    when, sep, action = (param or "").partition("|")
    action = action.strip().strip("[]")
    if action.upper().startswith("ACTION:"):
        action = action[7:]
    name = action.split(":", 1)[0].strip().lower()
    if not sep or not name:
        return "Не зрозуміла, що саме й коли зробити."
    if not name.startswith(LATER_OK):
        log.warning(f"Відкладена дія: «{action}» наперед не ставлю")
        return "Таке наперед не ставлю. Нагадати можу, а зробити скажи сам, коли буде час."
    remind_at = parse_remind_time(when.strip())
    if remind_at is None:
        return "Не зрозуміла, коли це зробити. Скажи, наприклад, через 30 хвилин або о 18:00."
    notes = _load_notes()
    new_id = max((n["id"] for n in notes), default=0) + 1
    text = _later_text(action)
    notes.append({"id": new_id, "text": text, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                  "remind_at": remind_at, "done": False, "action": action})
    _save_notes(notes)
    log.info(f"Відкладена дія #{new_id}: {action} ({remind_at})")
    return f"Добре, {vr.describe_remind(remind_at)} {text}."


def note_list() -> str:
    """Повертає список активних нотаток."""
    notes = [n for n in _load_notes() if not n["done"]]
    if not notes:
        return "Нотаток немає."
    lines = []
    for n in notes:
        remind = f" (нагадування {vr.describe_remind(n['remind_at'])})" if n.get("remind_at") else ""
        lines.append(f"{n['id']}. {n['text']}{remind}")
    return "Твої плани: " + ". ".join(lines)


def note_done(note_id: int) -> str:
    """Відмічає нотатку як виконану."""
    notes = _load_notes()
    for n in notes:
        if n["id"] == note_id:
            n["done"] = True
            _save_notes(notes)
            log.info(f"Нотатка #{note_id} виконана")
            return f"Відмітила план «{n['text']}» як виконаний."
    return f"Нотатку #{note_id} не знайдено."


def note_delete(note_id: int) -> str:
    """Видаляє нотатку."""
    notes = _load_notes()
    before = len(notes)
    notes = [n for n in notes if n["id"] != note_id]
    if len(notes) < before:
        _save_notes(notes)
        log.info(f"Нотатка #{note_id} видалена")
        return f"Видалила нотатку #{note_id}."
    return f"Нотатку #{note_id} не знайдено."


def note_clear(which: str = "all") -> str:
    """
    Очищає плани. which:
      'all'  — видалити всі;
      'done' — видалити тільки виконані;
      'найстаріший за N днів' не підтримується тут.
    """
    notes = _load_notes()
    before = len(notes)
    if before == 0:
        return "Планів і так немає."
    if which == "done":
        kept = [n for n in notes if not n.get("done")]
        removed = before - len(kept)
        if removed == 0:
            return "Виконаних планів немає."
        _save_notes(kept)
        log.info(f"Очищено виконаних планів: {removed}")
        return f"Прибрала {vr.count(removed, 'виконаний план', 'виконані плани', 'виконаних планів')}."
    # all
    _save_notes([])
    log.info(f"Очищено всі плани: {before}")
    return f"Очистила всі плани, прибрала {vr.count(before, 'план', 'плани', 'планів')}."


def reminder_loop():
    """Фоновий поток — перевіряє нагадування кожні 30 секунд."""
    log.info("Reminder loop запущено")
    while True:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            notes = _load_notes()
            fired_ids = []
            due = [n for n in notes if n.get("remind_at") and not n["done"] and n["remind_at"] <= now_str]
            for n in due:
                fired_ids.append(n["id"])
            if fired_ids:
                # Спершу прибрати, потім виконувати: дія може тривати (читання листа),
                # а цикл за 30 с не має запустити її вдруге
                _save_notes([n for n in notes if n["id"] not in fired_ids])
            for n in due:
                if n.get("action"):
                    log.info(f"Відкладена дія спрацювала: {n['action']}")
                    tts.speak(f"Як домовлялись, {n['text']}.")
                    from raphael import actions          # actions сам імпортує notes
                    actions.execute_action(n["action"])
                else:
                    log.info(f"Нагадування спрацювало (видаляю план): {n['text']}")
                    tts.speak(f"Нагадую: {n['text']}")
        except Exception as e:
            log.error(f"Reminder loop помилка: {e}")
        time.sleep(30)


def load_memory() -> dict:
    if os.path.exists(cfg.MEMORY_PATH):
        with open(cfg.MEMORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"user_name": "", "last_session": "", "facts": [], "session_count": 0}


def save_memory(data: dict):
    _write_json_atomic(cfg.MEMORY_PATH, data)


# memory.json тепер пишуть кілька потоків (підсумки щоденника, довіра, кінець сесії),
# і «прочитав, змінив, записав» без замка губив би чужі зміни
_memory_lock = threading.RLock()


def update_memory(change) -> dict:
    """change(mem) міняє памʼять на місці; читання й запис під одним замком."""
    with _memory_lock:
        mem = load_memory()
        change(mem)
        save_memory(mem)
        return mem


def update_memory_after_session(exchanges: list):
    """Зберігає короткий підсумок сесії через Groq."""
    # Без системного промпту: раніше він (8 тис. символів) ішов у підсумок
    # разом з розмовою, коли обмінів було мало
    exchanges = [m for m in exchanges if m.get("role") in ("user", "assistant")]
    if len(exchanges) < 3:
        return
    try:
        summary_prompt = (
            "Зроби короткий підсумок (1-2 речення) цієї розмови для майбутньої пам'яті. "
            "Вкажи що робив користувач, про що говорили, важливі факти. "
            "Відповідь — тільки підсумок без пояснень:\n\n"
            + "\n".join([f"{m['role']}: {m['content']}" for m in exchanges[-10:]])
        )
        resp = llm.llm_chat(
            llm.main_model(),
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=300,          # міркувальні моделі з'їдають частину ліміту
        )
        summary = resp.choices[0].message.content.strip()

        def keep(mem):
            mem["last_session"] = summary
            mem["session_count"] = mem.get("session_count", 0) + 1
        update_memory(keep)                 # читаємо після моделі: поки вона думала, памʼять могла змінитись
        log.info(f"Пам'ять збережена: {summary}")
    except Exception as e:
        log.error(f"Помилка збереження пам'яті: {e}")
