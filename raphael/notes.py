"""
Нотатки з нагадуваннями (notes.json) і памʼять між сесіями (memory.json).
"""
import json
import logging
import os
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
            # Раніше тут мовчки записувалась звичайна нотатка, а Рафаель казав
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
            for n in notes:
                if n.get("remind_at") and not n["done"] and n["remind_at"] <= now_str:
                    log.info(f"Нагадування спрацювало (видаляю план): {n['text']}")
                    tts.speak(f"Нагадую: {n['text']}")
                    fired_ids.append(n["id"])
            if fired_ids:
                # Видаляємо плани що спрацювали — щоб не повторювались
                notes = [n for n in notes if n["id"] not in fired_ids]
                _save_notes(notes)
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


def update_memory_after_session(exchanges: list):
    """Зберігає короткий підсумок сесії через Groq."""
    # Без системного промпту: раніше він (8 тис. символів) ішов у підсумок
    # разом з розмовою, коли обмінів було мало
    exchanges = [m for m in exchanges if m.get("role") in ("user", "assistant")]
    if len(exchanges) < 3:
        return
    try:
        mem = load_memory()
        summary_prompt = (
            "Зроби короткий підсумок (1-2 речення) цієї розмови для майбутньої пам'яті. "
            "Вкажи що робив користувач, про що говорили, важливі факти. "
            "Відповідь — тільки підсумок без пояснень:\n\n"
            + "\n".join([f"{m['role']}: {m['content']}" for m in exchanges[-10:]])
        )
        resp = llm.llm_chat(
            cfg.GROQ_PRIMARY_MODEL,
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=300,          # міркувальні моделі з'їдають частину ліміту
        )
        summary = resp.choices[0].message.content.strip()
        mem["last_session"] = summary
        mem["session_count"] = mem.get("session_count", 0) + 1
        save_memory(mem)
        log.info(f"Пам'ять збережена: {summary}")
    except Exception as e:
        log.error(f"Помилка збереження пам'яті: {e}")
