"""
Щоденник розмов: памʼять Лін між запусками.

Кожен обмін (що сказав Влад і що відповіла Лін) дописується в DIARY_DIR/РРРР-ММ-ДД.jsonl.
Тека локальна, поза git і поза сховищем: там розшифровки всього сказаного вголос.
З щоденника:
  • після перезапуску в розмову вертаються останні репліки, якщо говорили щойно;
  • за кожен минулий день модель пише 2-3 речення підсумку (memory.json, "days"),
    і останні з них ідуть у системний промт: Лін памʼятає, що було вчора;
  • diary_find:питання шукає в щоденнику за словами і відповідає голосом.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

from raphael import emotion
from raphael import llm
from raphael import notes
from raphael import runtime
from raphael import settings as cfg
from raphael import tts

log = logging.getLogger("Лін")

_lock = threading.Lock()

KEEP_SUMMARIES = 14      # днів підсумків у memory.json
PROMPT_DAYS = 2          # скільки останніх днів іде в системний промт
PROMPT_DAY_CHARS = 250   # і скільки символів з кожного (промт іде в кожен запит)
RESUME_HOURS = 3         # після перезапуску вертаємо розмову, якщо вона була не давніше
RESUME_TURNS = 3         # і стільки останніх обмінів
SUMMARY_EVERY = 3 * 3600


def _path(day: str) -> str:
    return os.path.join(cfg.DIARY_DIR, f"{day}.jsonl")


def _clean(text: str) -> str:
    """Без тегів настрою й дій: у щоденнику лише сказане."""
    text = llm._ACTION_RE.sub("", emotion.EMO_RE.sub("", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def add(user: str, reply: str, when: datetime = None) -> None:
    """Дописати обмін у файл дня. Тихо: щоденник не має ламати розмову."""
    when = when or datetime.now()
    row = {"t": when.strftime("%H:%M"), "user": (user or "").strip(), "lin": _clean(reply)}
    if not row["user"]:
        return
    try:
        with _lock:
            os.makedirs(cfg.DIARY_DIR, exist_ok=True)
            with open(_path(when.strftime("%Y-%m-%d")), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        log.debug(f"Щоденник: {e}")


def read_day(day: str) -> list:
    """Обміни за день; зіпсований рядок пропускаємо, а не весь день."""
    rows = []
    try:
        with open(_path(day), encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def recent_turns(now: datetime = None) -> list:
    """Останні обміни, якщо розмова була не давніше RESUME_HOURS: історія після перезапуску."""
    now = now or datetime.now()
    turns = []
    for back in (1, 0):                              # учора: розмова могла йти через північ
        day = now - timedelta(days=back)
        for r in read_day(f"{day:%Y-%m-%d}"):
            try:
                t = datetime.strptime(f"{day:%Y-%m-%d} {r['t']}", "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if timedelta(0) <= now - t <= timedelta(hours=RESUME_HOURS):
                turns.append([{"role": "user", "content": r["user"]},
                              {"role": "assistant", "content": r.get("lin", "")}])
    return [m for pair in turns[-RESUME_TURNS:] for m in pair]


# ── Підсумки днів ────────────────────────────────────────────────────────────

def _trim(text: str, limit: int) -> str:
    """Не довше limit символів, обрізаючи по кінцю речення, а не посеред слова."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + "…"


def summarize(day: str, rows: list) -> str:
    if len(rows) < 3:
        # Кілька реплік модель не підсумовує, а пише «без повного тексту не можу»
        # (перевірка 2026-11-03). Тут вистачить самих реплік
        return _trim("Коротко: " + "; ".join(f"«{r.get('user', '')}»" for r in rows) + ".", PROMPT_DAY_CHARS)
    talk = "\n".join(f"{r.get('t', '')} Людина: {r.get('user', '')}\n{r.get('t', '')} Лін: {r.get('lin', '')}"
                     for r in rows)[-6000:]
    resp = llm.llm_chat(llm.main_model(), messages=[{"role": "user", "content": (
        f"Це розмови з голосовою помічницею Лін (вона, жіночий рід) за {day}. Говорить зазвичай її власник Влад, але "
        "інколи поруч інші люди (його дівчина, друзі): якщо з реплік видно, що говорить хтось інший, "
        "так і пиши, не приписуй їхні слова Владу. Напиши 1-2 короткі речення українською (до 250 "
        "символів) для памʼяті Лін: чим займались, що просили, що важливого сказали про себе, плани чи "
        "людей, що лишилось незробленим. Без вступу й оцінок, лише факти. Якщо там лише дрібні "
        "команди (музика, таймер), так і напиши одним реченням.\n\n" + talk)}],
        max_tokens=400, temperature=0.3)
    return _trim(re.sub(r"\s+", " ", resp.choices[0].message.content or "").strip(), 400)


def summarize_pending(today: date = None) -> int:
    """Підсумки за минулий тиждень, яких ще нема в memory.json. → скільки написано."""
    today = today or date.today()
    done = 0
    for back in range(1, 8):
        day = (today - timedelta(days=back)).isoformat()
        if day in (notes.load_memory().get("days") or {}):
            continue
        rows = read_day(day)
        if not rows:
            continue
        text = summarize(day, rows)
        if not text:
            break                                    # модель промовчала: спробуємо пізніше
        mem = notes.load_memory()                    # могла змінитись, поки модель думала
        days = dict(mem.get("days") or {}, **{day: text})
        mem["days"] = dict(sorted(days.items())[-KEEP_SUMMARIES:])
        notes.save_memory(mem)
        log.info(f"Щоденник: підсумок за {day}: {text}")
        done += 1
    return done


def diary_loop():
    """Раз на кілька годин дописує підсумки минулих днів. У грі чекає: там мозок у
    хмарі з лімітом за хвилину, а підсумок не терміновий."""
    time.sleep(90)                                   # спершу хай завантажиться модель
    while True:
        if not runtime.GAME:
            try:
                summarize_pending()
            except Exception as e:
                log.warning(f"Щоденник: підсумок не вийшов: {e}")
        time.sleep(SUMMARY_EVERY)


def _day_name(day: str, today: date) -> str:
    ago = (today - date.fromisoformat(day)).days
    return {1: "вчора", 2: "позавчора"}.get(ago, f"{date.fromisoformat(day):%d.%m}")


def prompt_block(today: date = None) -> str:
    """Останні підсумки днів для системного промту."""
    today = today or date.today()
    days = notes.load_memory().get("days") or {}
    recent = [(d, t) for d, t in sorted(days.items()) if d < today.isoformat()][-PROMPT_DAYS:]
    if not recent:
        return ""
    parts = [f"{_day_name(d, today)}: {_trim(t, PROMPT_DAY_CHARS)}" for d, t in reversed(recent)]
    return ("СПОГАДИ (згадуй до речі, коли це в тему, не переказуй без потреби): "
            + " ".join(parts) + " ")


# ── Пошук у щоденнику ────────────────────────────────────────────────────────

_WORD = re.compile(r"[\wʼ'’]{4,}")
# Слова самого питання про минуле, а не його тема (основи з 5 літер)
_STOP = {"казав", "казал", "говор", "сказа", "розмо", "памʼя", "пам'я", "памя", "згада", "нагад",
         "вчора", "позав", "сьогод", "сього", "минул", "тижня", "тижде", "тиждн", "минуло",
         "ліна", "мені", "тобі", "коли", "щось", "якийс", "якось", "було", "були", "була",
         "про", "чого", "чому", "який", "яка", "яке", "які"}
_DAY_WORDS = (("позавчора", 2), ("вчора", 1), ("сьогодні", 0))


def _stems(text: str) -> set:
    return {w.lower()[:5] for w in _WORD.findall(text or "")}


def _score(want: set, text: str) -> int:
    """Скільки основ запиту є в тексті. Основа може бути коротша за 5 літер
    («рейд»), тож рахуємо початок слова, а не рівність: «рейд» є в «рейдимо»."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    return sum(any(w.startswith(q) for w in words) for q in want)


def find(query: str, days: int = 30, limit: int = 12, today: date = None) -> list:
    """Обміни, де найбільше слів запиту (за основою з 5 літер). «вчора», «позавчора»,
    «сьогодні» звужують пошук до дня, а без інших слів беруть увесь той день.
    → [(день, рядок)] за часом."""
    today = today or date.today()
    q = (query or "").lower()
    only = next((back for word, back in _DAY_WORDS if word in q), None)
    want = _stems(q) - _STOP
    backs = [only] if only is not None else range(days)
    scored = []
    for back in backs:
        day = (today - timedelta(days=back)).isoformat()
        for i, r in enumerate(read_day(day)):
            score = _score(want, f"{r.get('user', '')} {r.get('lin', '')}") if want else 1
            if score:
                scored.append((score, back, day, i, r))
    best = sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]       # більше збігів, потім свіжіше
    return [(day, r) for _, _, day, i, r in sorted(best, key=lambda x: (x[2], x[3]))]


def ask(question: str) -> None:
    """Відповісти голосом, що було раніше, за щоденником і підсумками днів."""
    def _run():
        try:
            rows = find(question)
            days = notes.load_memory().get("days") or {}
            if not rows and not days:
                tts.speak("Не памʼятаю такого. У щоденнику про це нічого.")
                return
            talk = "\n".join(f"{d} {r.get('t', '')} Влад: {r.get('user', '')} | Лін: {r.get('lin', '')}"
                             for d, r in rows)
            summ = "\n".join(f"{d}: {t}" for d, t in sorted(days.items())[-7:])
            log.info(f"Щоденник: «{question}» → {len(rows)} обмінів")
            answer = tts.speak_stream(llm.llm_stream(llm.main_model(), messages=[{"role": "user", "content": (
                f"Ти Лін, голосова помічниця Влада (про себе в жіночому роді, з ним на «ти»). Сьогодні "
                f"{date.today():%d.%m.%Y}. Влад питає голосом: «{question}» (розпізнано з мовлення, може "
                "бути з помилками). Відповідай від себе, як спогад, 1-2 речення українською, спираючись "
                "ТІЛЬКИ на записи нижче. Чого там нема, "
                "того не вигадуй, так і скажи.\n\nПідсумки днів:\n" + (summ or "(нема)")
                + "\n\nЗнайдені розмови:\n" + (talk or "(нема)"))}],
                max_tokens=400, temperature=0.3))
            log.info(f"Щоденник: відповідь: {answer[:120]}")
        except Exception as e:
            log.error(f"Щоденник: не згадала: {e}", exc_info=True)
            tts.speak("Не змогла згадати, щось зламалось.")

    threading.Thread(target=_run, daemon=True).start()
