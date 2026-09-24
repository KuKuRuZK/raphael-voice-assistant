"""
Другий мозок, сховище Obsidian: запис думок, пошук нотаток, відповіді за ними.
"""
import logging
import os
import re
import threading
import time
from datetime import datetime

from raphael import llm
from raphael import settings as cfg
from raphael import tts

log = logging.getLogger("Лін")


BRAIN_INBOX = os.path.join(cfg.BRAIN_VAULT, "Вхідні.md")

# Куди саме класти надиктоване. Ключ — як це називає Влад голосом.
BRAIN_TARGETS = {
    "вхідні": "Вхідні.md",
    "плани":  "Плани.md",
    "ідеї":   "Ідеї.md",
}

_BRAIN_HEADER = (
    "---\n"
    "type: inbox\n"
    "tags: [вхідні]\n"
    "updated: {date}\n"
    "summary: Надиктоване голосом через Рафаеля. Розбирається потім у нотатки.\n"
    "---\n\n"
    "# {title}\n\n"
    "Сюди Рафаель дописує те, що надиктовано на ходу. Правил немає, формату немає.\n\n"
)


def brain_capture(text: str, target: str = "вхідні") -> str:
    """Дописує рядок у потрібний файл сховища: вхідні, плани або ідеї."""
    text = (text or "").strip()
    if not text:
        return "А що саме записати? Я нічого не розчула."
    if not os.path.isdir(cfg.BRAIN_VAULT):
        log.error(f"Brain: сховища немає за шляхом {cfg.BRAIN_VAULT}")
        return "Не знайшла сховище на диску. Записати нікуди."

    key = (target or "вхідні").strip().lower()
    fname = BRAIN_TARGETS.get(key, BRAIN_TARGETS["вхідні"])
    path = os.path.join(cfg.BRAIN_VAULT, fname)
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_BRAIN_HEADER.format(date=datetime.now().strftime("%Y-%m-%d"),
                                             title=fname[:-3]))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- **{datetime.now().strftime('%Y-%m-%d %H:%M')}** — {text}\n")
        log.info(f"Brain capture [{fname}]: {text[:80]}")
        where = {"плани": "у плани", "ідеї": "в ідеї"}.get(key, "у вхідні")
        return f"Записала {where}: «{text}»."
    except Exception as e:
        log.error(f"Brain capture помилка: {e}")
        return "Не вийшло записати, файл не піддався."


_brain_index_cache = None
_brain_index_time  = 0.0
_BRAIN_INDEX_TTL   = 300          # перечитувати список нотаток раз на 5 хв


def _brain_notes() -> list:
    """Список нотаток сховища: назва, аліаси, summary. Кешується."""
    global _brain_index_cache, _brain_index_time
    now = time.monotonic()
    if _brain_index_cache is not None and now - _brain_index_time < _BRAIN_INDEX_TTL:
        return _brain_index_cache
    items = []
    try:
        for fn in sorted(os.listdir(cfg.BRAIN_VAULT)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(cfg.BRAIN_VAULT, fn)
            summary, aliases = "", ""
            try:
                with open(path, encoding="utf-8") as f:
                    head = f.read(1500)
            except Exception:
                continue
            for line in head.split("\n")[:15]:
                low = line.lower()
                if low.startswith("summary:"):
                    summary = line.split(":", 1)[1].strip()
                elif low.startswith("aliases:"):
                    aliases = line.split(":", 1)[1].strip(" []")
            items.append({"name": fn[:-3], "summary": summary,
                          "aliases": aliases, "path": path})
    except Exception as e:
        log.error(f"Brain index: {e}")
    _brain_index_cache, _brain_index_time = items, now
    return items


# Питальні й службові слова: довші за 3 літери, але сенсу для пошуку не несуть.
_BRAIN_STOP = {
    "який", "яка", "яке", "які", "коли", "куди", "чому", "хто", "що",
    "мене", "мені", "мого", "моя", "мої", "твій", "цей", "цього", "там", "тут",
    "треба", "можна", "скажи", "розкажи", "покажи", "нагадай", "такий", "така",
    "таке", "було", "буде", "щось", "чогось", "лежать", "лежить", "зараз",
}


def _brain_find(query: str, limit: int = 3) -> list:
    """
    Шукає нотатки за назвою, аліасами, summary, а якщо не вийшло — по тілу.
    Порівнюємо ОСНОВИ слів, бо українська відмінює: «магазину» має знайти «магазин».
    """
    q = (query or "").lower().strip()
    words = [w for w in re.split(r"\W+", q) if len(w) > 3 and w not in _BRAIN_STOP]
    stems = [w[:5] for w in words]
    if not stems:
        return []

    # Прохід 1: тільки назва, аліаси, summary. Це найточніше й не залежить
    # від розміру нотатки.
    meta = []
    for it in _brain_notes():
        hay = f"{it['name']} {it['aliases']} {it['summary']}".lower()
        score = (10 if q in hay else 0) + sum(4 for st in stems if st in hay)
        if score:
            meta.append((score, it))
    meta.sort(key=lambda x: -x[0])

    # Впевнений збіг це або точна фраза, або більшість основ запиту. Одна
    # випадкова основа впевненістю не є.
    strong = bool(meta) and meta[0][0] >= max(10, 4 * max(1, len(stems) - 1))
    if strong:
        return [it for _, it in meta[:limit]]

    # Прохід 2: по тілу. Рахуємо кількість входжень (з обмеженням), інакше
    # довгі нотатки-індекси перемагають лише через свій розмір.
    #
    # Раніше цей прохід запускався ЛИШЕ коли перший не дав нічого. Через це
    # один слабкий випадковий збіг у метаданих блокував пошук по тілах:
    # запит з імʼям і прізвищем колеги знаходив нотатку Agentic UX (бо в її
    # summary є те саме імʼя) і ніколи не доходив до «Люди», де про колегу
    # власне й написано. Тепер слабкий результат першого проходу не зупиняє,
    # а лише додається до другого.
    body_scored = []
    for it in _brain_notes():
        try:
            with open(it["path"], encoding="utf-8") as f:
                body = f.read().lower()
        except Exception:
            continue
        score = sum(min(body.count(st), 5) for st in stems)
        if score:
            body_scored.append((score, it))

    merged = {}
    for score, it in body_scored:
        merged[it["name"]] = (score, it)
    for score, it in meta:                      # метадані цінніші за тіло
        prev = merged.get(it["name"], (0, it))[0]
        merged[it["name"]] = (prev + score, it)
    ranked = sorted(merged.values(), key=lambda x: -x[0])
    return [it for _, it in ranked[:limit]]


def brain_read(name: str) -> str:
    """Коротко: про що нотатка. Без звертання до моделі."""
    found = _brain_find(name, limit=1)
    if not found:
        return f"Не знайшла нотатки про {name}."
    it = found[0]
    return f"{it['name']}. {it['summary']}" if it["summary"] else f"Нотатка {it['name']} є, але без опису."


def brain_ask(question: str) -> None:
    """Відповідає на питання, спираючись на нотатки сховища."""
    def _run():
        try:
            found = _brain_find(question)
            if not found:
                tts.speak("Не знайшла нічого в мозку по цьому.")
                return
            parts = []
            for it in found:
                try:
                    with open(it["path"], encoding="utf-8") as f:
                        parts.append(f"### {it['name']}\n{f.read()[:2500]}")
                except Exception:
                    continue
            if not parts:
                tts.speak("Нотатки знайшла, але прочитати не змогла.")
                return
            log.info(f"Brain ask '{question}': {[i['name'] for i in found]}")
            resp = llm.llm_chat(
                cfg.GROQ_PRIMARY_MODEL,
                # Задача сформульована як «перекажи, що є по темі», а НЕ як
                # «відповідай на питання». Причина: запит приходить із
                # розпізнавання мови й часто є уламком («по контрактах»).
                # На такому уламку модель не знаходила буквальної відповіді
                # й казала «в нотатках нічого немає», хоча тримала перед собою
                # рівно ті нотатки, які треба.
                messages=[{"role": "user", "content": (
                    # Без цього уточнення модель плутала Влада з колегою-тезком,
                    # який згадується в нотатках, і склеювала їх в одну людину.
                    "«Влад» це Владислав Собакар, власник цих нотаток. Усі інші люди, "
                    "згадані в нотатках, це його колеги й знайомі, а не він сам. "
                    "Ніколи не приписуй йому чужого прізвища.\n\n"
                    f"Влад питає голосом про: «{question}». Запит розпізнаний з мовлення, "
                    "тому може бути обірваним або з помилками, не чіпляйся до формулювання.\n\n"
                    "Нижче нотатки з його сховища, знайдені за цією темою. Стисло, 2-3 речення "
                    "українською, перекажи головне саме по цій темі: у якому стані справа, "
                    "що вирішено, що далі. Спирайся ТІЛЬКИ на нотатки, не вигадуй.\n"
                    "Відповідай «у нотатках про це нічого немає» лише тоді, коли теми там "
                    "справді немає.\n\n" + "\n\n".join(parts)
                )}],
                max_tokens=220,
                temperature=0.3,
            )
            answer = resp.choices[0].message.content.strip()
            log.info(f"Brain answer: {answer[:120]}")
            tts.speak(answer)
        except Exception as e:
            log.error(f"Brain ask помилка: {e}", exc_info=True)
            tts.speak("Не вийшло подивитись у мозок.")

    threading.Thread(target=_run, daemon=True).start()
