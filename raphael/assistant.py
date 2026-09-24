"""
Головний цикл і розмова: чекання імені, режими (звичайний, розмова,
диктування), швидкі команди без моделі, запит до моделі, підтвердження
небезпечних дій.
"""
import logging
import random
import re
import threading
import time
from datetime import datetime

import pyautogui

from raphael import actions
from raphael import agenda
from raphael import briefing
from raphael import dictation
from raphael import hotkeys
from raphael import llm
from raphael import notes
from raphael import pc
from raphael import prompt
from raphael import runtime
from raphael import settings as cfg
from raphael import stt
from raphael import sysmon
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


# ============================================================
#  РЕЖИМИ
# ============================================================
# normal    — слухає тільки після "Лін"
# chat      — відповідає на будь-яку фразу без wake word
# dictation — все що кажеш → вставляється в активне вікно
MODE   = "normal"

# Фрази «повтори»: voice_rules.REPEAT_PHRASES / is_repeat_request.

CHAT_MODE_TRIGGERS   = {
    "режим розмови", "chat mode", "розмовний режим", "говори зі мною", "speak mode",
    "почни розмову", "режим чату", "чат режим", "просто говори",
}
NORMAL_MODE_TRIGGERS = {
    "звичайний режим", "нормальний режим", "вийди з розмови", "стоп режим",
    "normal mode", "замовкни", "вийди з чату", "стоп чат", "зупини розмову",
    "завершити розмову", "виходь з режиму", "повернись в звичайний",
    "повернись до звичайного", "вимкни режим розмови",
}
DICTATION_TRIGGERS = {
    "режим диктування", "диктуй", "dictation", "диктування",
    "режим введення", "починай диктувати", "стартуй диктування",
}

FASTER_WORDS = {"швидше", "говори швидше", "faster", "прискорити", "швидший темп"}
SLOWER_WORDS = {"повільніше", "говори повільніше", "slower", "уповільнити", "повільний темп"}
RESET_SPEED_WORDS = {"нормальна швидкість", "звичайна швидкість", "нормальний темп",
                     "звичайний темп", "скинь швидкість"}

history = [{"role": "system", "content": prompt.SYSTEM_PROMPT}]  # оновлюється динамічно в ask_lin

# Відповідь на підтвердження: дія лише на ЧІТКЕ «так» без «ні», мовчання = ні
_YES_WORDS = {"так", "ага", "угу", "давай", "ок", "окей", "добре", "звичайно",
              "канєшно", "конєшно", "ясно", "поїхали", "жени", "да", "yes", "go"}
_NO_WORDS  = {"ні", "нє", "нєа", "відміна", "скасуй", "стоп", "нет", "no", "не"}


def _confirm(question: str, extra_yes=()) -> bool:
    tts.speak(question)
    ans = stt.listen(timeout=8, phrase_limit=5) or ""
    words = set(re.sub(r"[^\w\s']", " ", ans.lower()).split())
    ok = bool(words & (_YES_WORDS | set(extra_yes))) and not (words & _NO_WORDS)
    log.info(f"Підтвердження «{question}»: '{ans}' → {'так' if ok else 'ні'}")
    return ok


# Незворотні дії → (питання, додаткові слова згоди)
_MUST_CONFIRM = {
    "system_shutdown_pc": ("Вимкнути компʼютер? Скажи так або ні.", ("вимикай",)),
    "system_restart":     ("Перезавантажити компʼютер? Скажи так або ні.", ("перезавантажуй",)),
    "kill_process":       ("Закрити «{p}»? Скажи так або ні.", ("закривай", "вбивай")),
    "note_clear":         ("Стерти всі плани? Скажи так або ні.", ("стирай", "видаляй")),
}


def ask_lin(user_input: str) -> str:
    history.append({"role": "user", "content": user_input})
    log.info(f"Запит: '{user_input}'")
    # Оновлюємо system prompt з актуальною пам'яттю і нотатками
    history[0] = {"role": "system", "content": prompt.build_system_prompt()}
    # Обмежуємо розмір контексту: system + останні 10 повідомлень (5 обмінів)
    if len(history) > 11:
        history[1:] = history[-10:]

    # Основна модель, а резервна (інший провайдер) на БУДЬ-ЯКУ помилку:
    # 429, таймаут, 5xx, обрив мережі. Раніше резерв вмикався лише на 429.
    primary = llm._choose_model(user_input)
    secondary = cfg.GROQ_FALLBACK_MODEL if primary == cfg.GROQ_PRIMARY_MODEL else cfg.GROQ_PRIMARY_MODEL
    log.info(f"Модель: {primary}")
    try:
        response = llm.llm_chat(
            primary,
            messages=prompt.inject_time(history),
            fallback=secondary,
            # 180 було замало: міркувальні моделі частину ліміту витрачають на
            # думання і відповідь обривається. Сама відповідь однаково коротка:
            # модель зупиняється сама, зайвий ліміт нічого не коштує.
            max_tokens=700,
            temperature=0.7,
        )
    except llm.LLMUnavailable as e:
        history.pop()          # запит без відповіді не лишаємо в історії
        if e.rate_limited:
            tts.speak("Денний ліміт запитів вичерпано. Спробуй через кілька хвилин.")
        else:
            tts.speak("Моделі зараз не відповідають. Перевір інтернет або спробуй за хвилину.")
        return ""

    # content буває None чи порожнім (міркувальна модель витратила весь ліміт
    # на думання). Такого не можна класти в історію: наступний запит з
    # assistant-повідомленням без тексту провайдер може відхилити.
    reply = (response.choices[0].message.content or "").strip()
    log.info(f"Відповідь: '{reply}'")
    if not reply:
        history.pop()
        return "Хм, загубила думку. Повтори, будь ласка."
    history.append({"role": "assistant", "content": reply})

    # Три формати: [ACTION:type:param], [type:param] і голе type:param
    action_type, action_param, _bracketed = llm._find_action(reply)
    if action_type:
        # Модель інколи дублює дію звичайним текстом ПЕРЕД тегом:
        # «brain_plan:подивитись мені [ACTION:brain_plan:подивитись мені]».
        # Тег вирізається дужками, а гола копія лишалась і зачитувалась уголос.
        for dup in (f"{action_type}:{action_param}", f"{action_type}:"):
            if dup and dup in reply:
                reply = reply.replace(dup, " ")
        reply = re.sub(r"\s{2,}", " ", reply).strip()

        # ── Захист від небезпечних команд ──────────────────────────────────────
        DANGEROUS = {
            "system_lock":        ["заблокуй", "lock", "блокуй", "заблок"],
            "system_sleep":       ["сплячий", "sleep", "сон", "засни", "вимкни монітор"],
            "system_restart":     ["перезавантаж", "restart", "reboot", "ребут"],
            # Без голого «вимикай»: воно є і в «вимикай музику»
            "system_shutdown_pc": ["вимкни комп", "вимикай комп", "виключи комп", "вимкни пк",
                                   "вимикай пк", "вимкни ноут", "shutdown", "вимкнення комп"],
            # close_window: ширший список — раніше блокував легітимні запити
            "close_window":       ["закрий", "закрити", "close", "зупини програму",
                                   "вийди з програми", "закрий вікно"],
            # Деструктивні / керування вводом — лише за явним наміром у запиті
            # (захист від галюцинацій моделі, помилок STT і prompt-injection).
            "kill_process":       ["вбий", "вбити", "убий", "приший", "kill",
                                   "заверши процес", "закрий процес", "вимкни процес",
                                   "зніми процес"],
            "type_text":          ["надрукуй", "напиши", "введи", "набери", "впиши",
                                   "встав", "встави", "друкуй", "type"],
            "hotkey":             ["натисни", "натисніть", "клавіш", "комбінац",
                                   "hotkey", "shortcut", "гарячу"],
            "ask_claude":         ["клод", "claude", "клода", "клоді", "клодом"],
            # Стирання нотаток. 2026-08-15: фраза «видали все, що ти написала
            # до цього в екрані» дала note_clear і знесла ВСІ плани — бо цієї
            # дії тут не було. Тепер потрібна явна згадка нотаток чи планів.
            "note_clear":         ["план", "нотатк", "записи", "список"],
            "note_delete":        ["план", "нотатк", "запис"],
            "note_done":          ["план", "нотатк", "запис", "викона", "зробив", "готово"],
        }
        if action_type in DANGEROUS:
            keywords = DANGEROUS[action_type]
            if not any(kw in user_input.lower() for kw in keywords):
                log.warning(f"Заблоковано небезпечну дію [{action_type}] — запит не містить явного наміру: '{user_input}'")
                # Текст моделі («Вимикаю…») не зачитуємо: дія ж не виконана
                return "Цього не роблю: не почула явного прохання."
        # ───────────────────────────────────────────────────────────────────────

        # ── Веб-пошук: типово відповідаємо ГОЛОСОМ; вкладку — лише на явне прохання ──
        BROWSER_WORDS = ("браузер", "браузері", "вкладк", "хром", "chrome",
                         "сайт", "сторінк", "в гуглі")
        wants_browser = any(b in user_input.lower() for b in BROWSER_WORDS)
        rerouted_to_voice = False
        if action_type == "search_web" and not wants_browser:
            action_type = "web_search"        # тиха відповідь голосом, без вкладки
            rerouted_to_voice = True
        elif action_type == "web_search" and wants_browser:
            action_type = "search_web"        # явно просять браузер — відкриваємо вкладку

        # ── Незворотні дії: питаємо ЗАВЖДИ ─────────────────────────────────────
        # Навіть коли намір у фразі звучить явно: помиляється і модель, і
        # розпізнавання, а вимкнений компʼютер з незбереженою роботою чи стерті
        # плани не повернеш. Після дії текст моделі не зачитуємо, про результат
        # скаже сама дія.
        if action_type in _MUST_CONFIRM and not (
                action_type == "note_clear" and action_param.strip().lower() in ("done", "виконані", "виконане")):
            question, extra_yes = _MUST_CONFIRM[action_type]
            if not _confirm(question.format(p=action_param[:40]), extra_yes):
                tts.speak("Не чіпаю.")
                return ""
            log.info(f"Дія (підтверджено): [{action_type}:{action_param}]")
            actions.execute_action(f"[ACTION:{action_type}:{action_param}]")
            return ""

        # ── Підтвердження перед відкриттям ────────────────────────────────────
        # Якщо користувач САМ явно попросив (відкрий/запусти/знайди…) — НЕ перепитуємо:
        # це зайве тертя, і якщо «так» не розпізнається, дія марно зривається.
        CONFIRM_NEEDED = {"open_app", "open_url", "search_web", "open_youtube"}
        EXPLICIT_INTENT = ("відкрий", "відкри", "відчини", "запусти", "запуст",
                           "увімкни", "ввімкни", "вмикай", "включи", "врубай",
                           "запускай", "покажи", "знайди", "пошукай", "шукай",
                           "загугли", "відкривай", "глянь")
        explicit = any(e in user_input.lower() for e in EXPLICIT_INTENT)
        if cfg.CONFIRM_ACTIONS and action_type in CONFIRM_NEEDED and not explicit:
            label = action_param or action_type
            if not _confirm(f"Відкрити «{label[:60]}»? Скажи так або ні.",
                            ("відкривай", "відкрий", "запускай")):
                log.info(f"Дію [{action_type}:{action_param}] скасовано.")
                tts.speak("Скасовую.")
                return ""
        # ───────────────────────────────────────────────────────────────────────

        full_tag = f"[ACTION:{action_type}:{action_param}]"
        log.info(f"Дія: {full_tag}")
        runtime._action_failed.clear()
        actions.execute_action(full_tag)
        if runtime._action_failed.is_set():
            return ""          # дія вже сама чесно сказала, що не вийшло
        reply = llm._ACTION_RE.sub("", reply).strip()
        if rerouted_to_voice:
            return ""   # відповідь озвучить сам пошук — не дублюємо ack моделі

    return reply


# ============================================================
#  ОСНОВНИЙ ЦИКЛ (фоновий поток)
# ============================================================

def check_mode_change(text: str) -> bool:
    """Перевіряє чи є команда зміни режиму. Повертає True якщо режим змінився."""
    global MODE
    # Прибираємо wake word щоб не заважав порівнянню
    t = vr.strip_wake_words(text.lower(), cfg.WAKE_WORDS)

    # Перехід в dictation mode
    for trigger in DICTATION_TRIGGERS:
        if trigger in t:
            MODE = "dictation"
            log.info("Режим: DICTATION")
            if runtime.LIN_UI: runtime.LIN_UI.set_mode("dictation")
            tts.speak("Режим диктування. Говори — вставлятиму текст. Скажи 'стоп' щоб завершити.")
            return True

    # Перехід в chat mode
    for trigger in CHAT_MODE_TRIGGERS:
        if trigger in t:
            MODE = "chat"
            log.info("Режим: CHAT")
            if runtime.LIN_UI: runtime.LIN_UI.set_mode("chat")
            tts.speak("Режим розмови активовано. Говори — я слухаю. Скажи 'стоп' або 'нормальний режим' щоб вийти.")
            return True

    # Повернення в normal mode — точне співпадіння
    for trigger in NORMAL_MODE_TRIGGERS:
        if trigger in t:
            MODE = "normal"
            log.info("Режим: NORMAL (точне)")
            if runtime.LIN_UI: runtime.LIN_UI.set_mode("normal")
            tts.speak("Ок, виходжу.")
            return True

    # Нечітке співпадіння — тільки в chat mode, бо STT часто обрізає слова
    # ("нормальний режи", "звичайний реж", просто "стоп")
    if MODE == "chat":
        words = set(t.split())
        normal_words  = {"нормальний", "нормальн", "звичайний", "звичайн", "normal"}
        regime_words  = {"режим", "режи", "реж", "mode"}
        single_exits  = {"стоп", "досить", "все", "хватит", "хватить", "enough", "stop"}

        # Є слово типу "нормальний/звичайний" + будь-яке слово з "режим"
        if (words & normal_words) and (words & regime_words):
            MODE = "normal"
            log.info("Режим: NORMAL (нечітке: нормальний+режим)")
            tts.speak("Ок, виходжу.")
            return True

        # Просто "стоп", "досить" тощо як окрема фраза
        if t.strip() in single_exits:
            MODE = "normal"
            log.info("Режим: NORMAL (single exit)")
            tts.speak("Ок.")
            return True

    return False


def process_command(command: str):
    """Обробляє команду і відповідає."""
    log.info(f"Команда: '{command}'")
    print(f"\n  Ви:  {command}")

    # ── Швидкі відповіді без Groq ────────────────────────────
    cmd = command.lower().strip()

    # Повтори останню відповідь. Лише коротке «повтори / не почув / ще раз»:
    # раніше будь-яка фраза з «повтор» чи «ще раз» («повторюй трек», «вимкни
    # повтор») сюди потрапляла, і повтор у Spotify голосом був недосяжний.
    if vr.is_repeat_request(cmd):
        if tts._last_spoken:
            log.info("Повтор останньої відповіді")
            tts.speak(tts._last_spoken)
        else:
            tts.speak("Ще нічого не казала.")
        return

    # «Скасуй вимкнення» працює завжди і без моделі: за 30 с до вимкнення
    # чекати відповіді LLM ризиковано
    if vr.is_cancel_shutdown(cmd):
        pc._cancel_shutdown()
        return

    # Миттєві команди: пауза, наступний трек, гучність, час, погода. Точний
    # збіг усієї фрази, без запиту до моделі, тому спрацьовують одразу.
    instant = vr.instant_command(cmd)
    if instant:
        log.info(f"Миттєва команда: {instant}")
        if instant == "say_time":
            tts.speak(f"Зараз {datetime.now():%H:%M}.")
        else:
            actions.execute_action(f"[ACTION:{instant}:]")
            if runtime.LIN_UI:
                runtime.LIN_UI.safe_set_state("idle")    # пауза чи гучність мовчки: не лишати «Думаю»
        return

    # Швидкість голосу — тільки якщо це коротка команда (≤ 4 слова)
    if len(cmd.split()) <= 4:
        if any(w in cmd for w in FASTER_WORDS):
            tts.speak(tts._adjust_voice_rate("faster"))
            return
        if any(w in cmd for w in SLOWER_WORDS):
            tts.speak(tts._adjust_voice_rate("slower"))
            return
        if any(w in cmd for w in RESET_SPEED_WORDS):
            tts.speak(tts._adjust_voice_rate("reset"))
            return

    # Очищення планів голосом. Стирання ВСІХ планів незворотне, тому так само
    # через «так/ні», як і коли це вирішує модель (раніше тут стирало одразу).
    if any(w in cmd for w in ("очисти всі плани", "видали всі плани", "очисти плани",
                              "видали всі нотатки", "очисти список планів", "видали плани")):
        question, extra_yes = _MUST_CONFIRM["note_clear"]
        tts.speak(notes.note_clear("all") if _confirm(question, extra_yes) else "Не чіпаю.")
        return
    if any(w in cmd for w in ("очисти виконані", "видали виконані", "прибери виконані")):
        tts.speak(notes.note_clear("done"))
        return

    # Push-to-talk перемикач голосом
    if any(w in cmd for w in ("режим кнопки", "тільки по кнопці", "push to talk",
                              "слухай по кнопці", "активація кнопкою")):
        hotkeys._toggle_ptt(True)
        return
    if any(w in cmd for w in ("постійне слухання", "завжди слухай", "звичайне слухання",
                              "слухай завжди", "вимкни режим кнопки")):
        hotkeys._toggle_ptt(False)
        return

    # Буфер — прочитати без Groq
    if any(w in cmd for w in ("що в буфері", "прочитай буфер", "покажи буфер", "clipboard")):
        tts.speak(pc._clipboard_read())
        return

    # Екран — подивитись без Groq-роутингу
    SCREEN_TRIGGERS = ("що на екрані", "подивись на екран", "подивись екран",
                       "опиши екран", "що зараз на екрані", "глянь на екран",
                       "що відкрито", "дивись на екран")
    if any(w in cmd for w in SCREEN_TRIGGERS):
        threading.Thread(target=lambda: pc._screen_look(""), daemon=True).start()
        return

    # Здоров'я системи — без Groq-роутингу
    HEALTH_TRIGGERS = ("як справи з системою", "стан системи", "перевір систему",
                       "як система", "чи все ок з системою", "здоров'я системи",
                       "діагностика системи", "як комп")
    if any(w in cmd for w in HEALTH_TRIGGERS):
        threading.Thread(target=lambda: tts.speak(sysmon._system_health_report()), daemon=True).start()
        return

    # Ранковий дайджест на вимогу
    if any(w in cmd for w in ("дайджест", "огляд дня", "що по дню", "брифінг",
                              "розкажи про день", "підсумок дня")):
        threading.Thread(target=lambda: briefing.morning_briefing(greeting=False), daemon=True).start()
        return

    # Календар (читання) — без Groq-роутингу
    if any(w in cmd for w in ("що в мене сьогодні", "що сьогодні", "плани на сьогодні",
                              "що в мене на сьогодні", "розклад на сьогодні")):
        threading.Thread(target=lambda: agenda._calendar_agenda("today"), daemon=True).start()
        return
    if any(w in cmd for w in ("що в мене завтра", "що завтра", "плани на завтра",
                              "розклад на завтра")):
        threading.Thread(target=lambda: agenda._calendar_agenda("tomorrow"), daemon=True).start()
        return
    if any(w in cmd for w in ("плани на тиждень", "що на тиждень", "розклад на тиждень")):
        threading.Thread(target=lambda: agenda._calendar_agenda("week"), daemon=True).start()
        return

    try:
        reply = ask_lin(command)
        print(f"  Рафаель: {reply}\n")
        tts.speak(reply)
    except Exception as e:
        log.error(f"Помилка відповіді: {e}", exc_info=True)
        tts.speak("Вибачте, щось пішло не так.")


def _startup_greeting() -> str:
    """Вибирає привітання залежно від часу доби."""
    mem = notes.load_memory()
    name = mem.get("user_name", "").strip()
    n = f", {name}" if name else ""

    h = datetime.now().hour
    if 5 <= h < 11:
        variants = [
            f"Доброго ранку{n}.",
            f"О, вже прокинувся{n}?",
            f"Ранок{n}. Що маємо сьогодні?",
            f"Привіт{n}, ранкова зміна.",
        ]
    elif 11 <= h < 17:
        variants = [
            f"Привіт{n}.",
            f"О, вітаю{n}.",
            f"Ну що{n}, яка задача?",
            f"Слухаю{n}.",
        ]
    elif 17 <= h < 22:
        variants = [
            f"Добрий вечір{n}.",
            f"О, вечір{n}. Що трапилось?",
            f"Вечір{n}. Я тут.",
            f"Привіт{n}, чим можу?",
        ]
    else:
        variants = [
            f"О, ще не спиш{n}?",
            f"Пізно, але я тут{n}.",
            f"Нічна зміна{n}, зрозуміло.",
            f"Слухаю{n}, хоч і пізно.",
        ]
    return random.choice(variants)


def assistant_loop():
    global MODE
    try:
        log.info("=== Лін запущена ===")
        tts.speak(_startup_greeting())

        while True:
            try:
                # ── Поки триває окрема сесія диктовки, основний цикл мовчить ──
                # Інакше мікрофон слухають двоє: диктовка пише текст, а цей цикл
                # той самий текст віддає моделі як команду. 2026-08-15 через це
                # надиктований абзац одночасно і надрукувався, і потрапив у мозок.
                if dictation._typing_dictation.is_set() or dictation._brain_dictating.is_set():
                    time.sleep(0.3)
                    continue

                # ── PUSH-TO-TALK: чекаємо натискання клавіші замість постійного слухання ──
                if cfg.PUSH_TO_TALK:
                    if not hotkeys._ptt_event.wait(timeout=1.0):
                        continue          # клавішу не натиснули — чекаємо далі
                    hotkeys._ptt_event.clear()
                    try:
                        import winsound
                        winsound.Beep(660, 80)   # короткий сигнал «слухаю»
                    except Exception:
                        pass
                    command = stt.listen(timeout=8)   # одна команда без wake word
                    if command and not check_mode_change(command):
                        process_command(command)
                    continue

                # У normal-режимі це фонове чекання імені: вікно не показує
                # «Слухаю» і не виводить підслухане. З WAKE_ENGINE="vosk" імʼя
                # шукається локально, і фрази без нього взагалі не йдуть у хмару.
                waiting_name = MODE == "normal"
                text = None
                if waiting_name and cfg.WAKE_ENGINE == "vosk":
                    text = stt.wait_for_wake(timeout=30, interrupt=lambda: (
                        MODE != "normal" or cfg.PUSH_TO_TALK
                        or dictation._typing_dictation.is_set()
                        or dictation._brain_dictating.is_set()))
                if text is None:                      # локального детектора немає
                    text = stt.listen(timeout=30, passive=waiting_name)
                if not text:
                    continue

                # ── DICTATION MODE: все що кажеш → вставляється в активне вікно ──
                if MODE == "dictation":
                    # Вихід лише на окреме «стоп» чи «стоп диктування» в кінці:
                    # раніше «кінець тижня» чи «стопка» теж вимикали диктування
                    if vr.is_dictation_exit(text):
                        MODE = "normal"
                        if runtime.LIN_UI: runtime.LIN_UI.set_mode("normal")
                        tts.speak("Диктування зупинено.")
                        log.info("Режим: NORMAL (dictation exit)")
                        continue
                    # Голосова пунктуація цілими словами («команда» більше не стає «,нда»)
                    result = vr.apply_voice_punctuation(text)
                    # Вставляємо в активне вікно через буфер
                    import pyperclip as _pc
                    _saved = _pc.paste()
                    _pc.copy(result + " ")
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.25)   # чекаємо завершення вставки
                    _pc.copy(_saved)
                    log.info(f"Dictation: '{result[:60]}'")
                    if runtime.LIN_UI: runtime.LIN_UI.safe_set_state("listening", result[:50])
                    continue

                # ── CHAT MODE: слухаємо без wake word ──
                if MODE == "chat":
                    if check_mode_change(text):
                        continue
                    process_command(text)
                    continue

                # ── NORMAL MODE: чекаємо wake word ──
                # Імʼя цілим словом на початку фрази чи в кінці. Раніше шукалось
                # підрядком будь-де, і «хвилин», «лінія», «Берлін» будили Рафаеля.
                wake = vr.find_wake(text, cfg.WAKE_WORDS)
                if not wake:
                    continue
                command = wake[1]
                if runtime.LIN_UI:
                    runtime.LIN_UI.safe_set_state("thinking", command or text)

                # Перевірка зміни режиму прямо з wake word фрази
                if check_mode_change(command):
                    continue

                if not command:
                    tts.speak("Слухаю.")
                    command = stt.listen(timeout=8)

                if not command:
                    tts.speak("Не почула команду.")
                    continue

                process_command(command)

            except Exception as e:
                log.error(f"Помилка в циклі: {e}", exc_info=True)

    except Exception as e:
        log.critical(f"ФАТАЛЬНА ПОМИЛКА: {e}", exc_info=True)
