"""
Міст до ПК, домашній бік (етап 4): завдання від агента на сервері виконує домашня Лін.

Завдання приходять тунелем разом з повідомленнями (tg_lin._pull_from_hub), виконуються
в окремому потоці по одному, результат іде назад на хаб (POST /home/jobs).

Двері замикаємо тут, а не лише на сервері: сервер читав чужі сторінки, тож теоретично
його агента можна підбурити. Тому з сервера на ПК можна лише те, що в TIERS:
- "auto": робить сама (прочитати публічну сторінку, дописати у «Вхідні», сказати вголос);
- "ask": лише після кнопки «Так» у Telegram (глянути на екран: знімок бачить модель);
- вартовим вакансій сторінки з домашньої адреси, коли сайт не пускає сервер (теж "auto");
- усе інше, і мишка з клавіатурою теж, відмовляє без питань.
Нічого не видаляє, не надсилає, не запускає програм.
"""
import json
import logging
import queue
import threading

from raphael import settings as cfg

log = logging.getLogger("Лін")

TIERS = {"pc_read_page": "auto", "pc_note": "auto", "pc_say": "auto", "pc_look": "ask",
         "pc_page_text": "auto", "pc_page_links": "auto"}
RAW = {"pc_page_text", "pc_page_links"}     # JSON для вартового: свій ліміт, обрізати посередині не можна
RESULT_LIMIT = 6000         # стільки символів результату їде назад на сервер
POST_TRIES = 3

_jobs = queue.Queue()
_taken = set()              # id завдань цього запуску: хаб може надіслати те саме вдруге
_done = {}                  # id → результат: на повтор віддаємо його, а не робимо вдруге
_stop = threading.Event()


def classify(tool: str) -> str:
    """"auto", "ask" чи "deny" для дії, яку просить сервер."""
    return TIERS.get(tool, "deny")


def submit(job: dict) -> None:
    """Завдання з хаба в чергу. Те саме вдруге (результат тоді не дійшов) не виконуємо
    знову, лише віддаємо вже готовий результат; те, що саме робиться, пропускаємо."""
    jid = job.get("id")
    if jid in _done:
        _jobs.put({"id": jid, "cached": _done[jid]})
    elif jid not in _taken:
        _taken.add(jid)
        _jobs.put(job)


def _describe(tool: str, args: dict) -> str:
    from cloud import pcjobs
    return pcjobs.describe(tool, args)


def _read_page(job: dict) -> str:
    from cloud import tools                 # ті самі гальма, що й на сервері: лише публічні адреси
    return tools.read_page((job.get("args") or {}).get("url", ""))


def _look(job: dict) -> str:
    from raphael import pc
    question = (job.get("args") or {}).get("question", "")
    which = pc._which_monitor(question)
    return pc.describe_screen(question, "all" if which == "active" else which)


def _note(job: dict) -> str:
    from raphael import brain
    args = job.get("args") or {}
    return brain.brain_note(args.get("title", ""), args.get("text", ""), source=f"справа #{job.get('task_id')}")


def _say(job: dict) -> str:
    from raphael import emotion, tts
    _, text = emotion.take((job.get("args") or {}).get("text", ""))
    if not text.strip():
        return "Нема що сказати."
    tts.speak(text)
    return "Сказала вголос."


def _page_text(job: dict) -> str:
    """Текст сторінки для вартового вакансій, JSON {"text", "error"}."""
    from cloud import tools
    text, error = tools.page_text((job.get("args") or {}).get("url", ""), limit=15000)
    return json.dumps({"text": text, "error": error}, ensure_ascii=False)


def _page_links(job: dict) -> str:
    """Посилання сторінки зі списком вакансій, JSON {"links", "error"}."""
    from cloud import tools
    links, error = tools.page_links((job.get("args") or {}).get("url", ""))
    return json.dumps({"links": links[:400], "error": error}, ensure_ascii=False)


_RUN = {"pc_read_page": _read_page, "pc_look": _look, "pc_note": _note, "pc_say": _say,
        "pc_page_text": _page_text, "pc_page_links": _page_links}


def execute(job: dict, confirm=None) -> str:
    """Виконати одне завдання за правилами TIERS → що відповісти агентові на сервері."""
    tool, args = job.get("tool", ""), job.get("args") or {}
    tier = classify(tool)
    if tier == "deny" or tool not in _RUN:
        log.warning(f"Міст: сервер просив «{tool}», відмовила")
        return f"Відмовлено: «{tool}» з сервера на ПК не виконую."
    if tier == "ask":
        if confirm is None:
            from raphael import tg_lin
            confirm = tg_lin.confirm
        chat = cfg.TELEGRAM_ALLOWED_IDS[0] if cfg.TELEGRAM_ALLOWED_IDS else None
        if not confirm(f"💻 Справа #{job.get('task_id')} просить {_describe(tool, args)}. Дозволити?", chat=chat):
            return "Влад не дозволив (або не відповів за 2 хвилини). Без цього обійдись або спитай його."
    log.info(f"Міст: {_describe(tool, args)[:100]}")
    try:
        out = _RUN[tool](job) or "Готово."
        return out if tool in RAW else out[:RESULT_LIMIT]
    except Exception as e:
        log.error(f"Міст: {tool} впало: {e}", exc_info=True)
        return f"На ПК не вийшло: {type(e).__name__}."


def worker(post) -> None:
    """Виконує завдання по одному; post(id, результат) віддає на хаб. Не віддалось за
    POST_TRIES спроб: хаб сам надішле завдання вдруге (RESEND), і тоді піде той самий результат."""
    while not _stop.is_set():
        try:
            job = _jobs.get(timeout=1)
        except queue.Empty:
            continue
        result = job["cached"] if "cached" in job else execute(job)
        _done[job["id"]] = result
        for attempt in range(POST_TRIES):
            try:
                post(job["id"], result)
                break
            except Exception as e:
                log.warning(f"Міст: результат #{job['id']} не віддала ({type(e).__name__}), ще раз")
                _stop.wait(5 * (attempt + 1))


def stop() -> None:
    _stop.set()
