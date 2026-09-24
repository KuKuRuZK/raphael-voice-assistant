"""
Моделі: Groq, Gemini і Ollama за одним OpenAI-сумісним API, резервна
модель на будь-яку помилку, розбір тегів дій [ACTION:тип:параметр].
"""
import logging
import re

from raphael import settings as cfg

log = logging.getLogger("Лін")


# Слова що чітко вказують на команду-дію → можна швидкою моделлю 8b
_COMMAND_HINTS = (
    "відкрий", "відкрити", "закрий", "закрити", "запусти", "увімкни", "вимкни",
    "постав", "грай", "відтвори", "пауза", "зупини", "наступн", "попередн",
    "гучніше", "тихіше", "гучність", "голосніше", "тихше", "звук",
    "погода", "таймер", "нагадай", "скріншот", "заблокуй", "перезавантаж",
    "спотіфай", "spotify", "дискорд", "телеграм", "хром", "браузер",
    "що на екрані", "сховай", "згорни", "розгорни", "перемкни",
    "що в пошті", "плейлист", "шафл", "перемішай", "стеж за",
)

# Теми, де 8b помиляється дорого: або треба розібрати параметри (текст|час),
# або дія незворотна. 2026-08-15: «нагадай, через годину зробити перерву»
# пішло на 8b і повернулось як [note_done:3] — спроба закрити чужу нотатку.
_FORCE_PRIMARY = (
    "нагада", "нагадува",                      # note_remind з розбором часу
    "запиши", "занотуй", "нотатк", "план",     # note_add / brain_add
    "мозок", "вхідні", "обсідіан", "нотатц", "сховищ",
    "пиши", "друкуй", "надрукуй", "записуй",
    "видали", "видалит", "очисти", "прибери",  # незворотне
    "виконан", "зроблен", "готово",
)

# ── Провайдери LLM ───────────────────────────────────────────────────────────
# Groq, Gemini і локальні сервери (Ollama, LM Studio, llama.cpp) говорять ОДНИМ
# протоколом — OpenAI-сумісним. Тому провайдер це просто base_url + ключ, а не
# окремий SDK і окрема гілка коду на кожного.
#
# Модель пишеться як "провайдер:модель". Без префікса — Groq, щоб старі
# конфіги працювали без правок.
#   groq:openai/gpt-oss-120b   gemini:gemini-2.5-flash   local:llama3.2
LLM_PROVIDERS = {
    "groq":   {"base_url": "https://api.groq.com/openai/v1",
               "key_name": "GROQ_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_name": "GEMINI_API_KEY"},
    # Місце під майбутню локальну модель. Ollama слухає 11434, LM Studio 1234.
    "local":  {"base_url": "http://127.0.0.1:11434/v1",
               "key_name": "", "key_default": "ollama"},
}
_llm_clients = {}

# Таймаут одного запиту до моделі. Повтори SDK вимкнені (max_retries=0): за
# замовчуванням openai робить ще 2 спроби, і на мертвому провайдері це до
# півтори хвилини тиші. Швидше одразу піти на резервного провайдера.
LLM_TIMEOUT = 20


def _llm(model: str):
    """('провайдер:модель') → (клієнт, чиста назва моделі)."""
    prov, sep, bare = (model or "").partition(":")
    if not sep or prov not in LLM_PROVIDERS:
        prov, bare = "groq", model
    pcfg = LLM_PROVIDERS[prov]
    cli = _llm_clients.get(prov)
    if cli is None:
        from openai import OpenAI
        key = cfg._secret(pcfg["key_name"]) if pcfg.get("key_name") else ""
        if pcfg.get("key_name") and not key:
            log.warning(f"{pcfg['key_name']} не задано (secrets.json або змінна середовища): "
                        f"запити до {prov} не пройдуть, працюватиме лише резерв")
        cli = OpenAI(api_key=key or pcfg.get("key_default", "none"),
                     base_url=pcfg["base_url"], timeout=LLM_TIMEOUT, max_retries=0)
        _llm_clients[prov] = cli
        log.info(f"LLM-провайдер піднято: {prov}")
    return cli, bare


class LLMUnavailable(Exception):
    """Не відповіла ні основна модель, ні резервна. errors: [(модель, помилка)]."""
    def __init__(self, errors):
        super().__init__("; ".join(f"{m}: {e}" for m, e in errors))
        self.errors = errors

    @property
    def rate_limited(self) -> bool:
        return all(_is_rate_limit(e) for _, e in self.errors)


def _is_rate_limit(e) -> bool:
    s = str(e).lower()
    return "429" in s or "rate_limit" in s or "rate limit" in s or "quota" in s


def _fallback_for(model: str) -> str:
    """Резерв за замовчуванням: інша модель пари основна/резервна."""
    return cfg.GROQ_FALLBACK_MODEL if model == cfg.GROQ_PRIMARY_MODEL else cfg.GROQ_PRIMARY_MODEL


def _llm_call(model: str, messages, **kw):
    cli, bare = _llm(model)
    return cli.chat.completions.create(model=bare, messages=messages,
                                       **_model_kwargs(bare), **kw)


def llm_chat(model: str, messages, fallback=None, **kw):
    """
    Єдина точка виклику моделі. Сама підставляє параметри під провайдера.

    Якщо модель не відповіла з БУДЬ-ЯКОЇ причини (429, таймаут, 5xx, обрив
    мережі, модель прибрали), питаємо резервну на іншому провайдері.
    fallback: None → автоматично інша модель пари основна/резервна;
              False → без резерву (зір: на Groq моделей із зображеннями немає).
    Якщо не відповіла жодна, кидає LLMUnavailable.
    """
    try:
        return _llm_call(model, messages, **kw)
    except Exception as e:
        alt = _fallback_for(model) if fallback is None else fallback
        if not alt or alt == model:
            raise LLMUnavailable([(model, e)]) from e
        log.warning(f"LLM {model} не відповіла ({type(e).__name__}: {str(e)[:150]}), пробую {alt}")
        try:
            return _llm_call(alt, messages, **kw)
        except Exception as e2:
            log.error(f"LLM резерв {alt} теж не відповів: {type(e2).__name__}: {str(e2)[:150]}")
            raise LLMUnavailable([(model, e), (alt, e2)]) from e2


def _stream_text(stream):
    """Текстові шматки з потоку OpenAI chat.completions (міркування пропускаємо)."""
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue                      # службові шматки (usage тощо)
        piece = getattr(chunk.choices[0].delta, "content", None)
        if piece:
            yield piece


def llm_stream(model: str, messages, fallback=None, **kw):
    """
    Як llm_chat, але віддає текст шматками, поки модель його пише: для
    довгих відповідей, які одразу йдуть в озвучку (tts.speak_stream).

    Резерв можливий лише до першого шматка тексту. Якщо модель впала вже
    посеред відповіді, почате не повторюємо іншою моделлю: це звучало б як
    дві різні відповіді підряд. Потік тоді просто обривається винятком.
    """
    def _open(m):
        cli, bare = _llm(m)
        pieces = _stream_text(cli.chat.completions.create(
            model=bare, messages=messages, stream=True, **_model_kwargs(bare), **kw))
        return next(pieces, ""), pieces   # чекаємо перший шматок тексту

    try:
        first, rest = _open(model)
    except Exception as e:
        alt = _fallback_for(model) if fallback is None else fallback
        if not alt or alt == model:
            raise LLMUnavailable([(model, e)]) from e
        log.warning(f"LLM {model} не відповіла ({type(e).__name__}: {str(e)[:150]}), пробую {alt}")
        try:
            first, rest = _open(alt)
        except Exception as e2:
            log.error(f"LLM резерв {alt} теж не відповів: {type(e2).__name__}: {str(e2)[:150]}")
            raise LLMUnavailable([(model, e), (alt, e2)]) from e2
    if first:
        yield first
    yield from rest


# Розбір тега дії. Терпимий до пробілів і регістру: gpt-oss інколи пише
# "[ ACTION:spotify_pause: ]", і сувора версія такий тег просто не бачила.
_ACTION_RE = re.compile(r"\[\s*(?:ACTION\s*:\s*)?([A-Za-z_]+)\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)

# Модель регулярно пише дію БЕЗ дужок: «brain_plan:купити молоко».
# Рішення при цьому правильне — гине лише формат. Тому ловимо й такий вигляд,
# але тільки на початку відповіді й лише для відомих префіксів дій,
# щоб звичайний текст із двокрапкою не став дією.
_BARE_ACTION_RE = re.compile(r"^\s*([a-z][a-z_]{2,24})\s*:\s*(.*)$", re.S)
_ACTION_PREFIXES = (
    "brain_", "note_", "dictate_", "spotify_", "open_", "search_", "web_",
    "clipboard_", "calendar_", "gmail_", "slack_", "system_", "monitor_",
    "screen_", "focus_", "memory_", "volume_", "window_", "notepad_",
    "claude_", "ask_", "kill_", "type_", "voice_", "timer", "weather",
    "currency", "hotkey", "shutdown", "get_time", "minimize_", "close_",
)


def _find_action(reply: str):
    """Шукає дію спершу в дужках, потім у голому вигляді. → (тип, параметр, чи_в_дужках)."""
    m = _ACTION_RE.search(reply)
    if m:
        return m.group(1).lower(), m.group(2).strip(), True
    m = _BARE_ACTION_RE.match(reply or "")
    if m:
        name = m.group(1).lower()
        if name.startswith(_ACTION_PREFIXES) or name in ("weather", "timer", "hotkey"):
            log.info(f"Дія без дужок, приймаю: {name}")
            return name, m.group(2).strip(), False
    return None, None, False


def _model_kwargs(model: str) -> dict:
    """
    Параметри під конкретну модель.

    gpt-oss і qwen3.6 — МІРКУВАЛЬНІ. Без обмеження міркувань вони витрачають
    увесь ліміт токенів на «думання» і повертають порожню відповідь або
    обрізаний тег дії. Перевірено 2026-08-15 при міграції з llama-3.3.
    """
    m = (model or "").lower()
    if m.startswith("openai/gpt-oss"):
        # ТІЛЬКИ low. На medium модель думає над кожним словом величезного
        # системного промта: у бою це дало 30 СЕКУНД на одну відповідь
        # (лог 2026-08-15, 16:47:00 → 16:47:30). У коротких тестах medium
        # виглядав нормально саме тому, що там промт був крихітний.
        return {"reasoning_effort": "low"}
    if m.startswith("qwen/"):
        return {"reasoning_effort": "none"}
    if "gemini-3.7" in m or "-pro" in m:
        # Ці беремо САМЕ заради міркувань — не глушимо їх.
        return {"reasoning_effort": "low"}
    if "gemini-3" in m or "gemini-2" in m:
        # Решта Gemini теж міркує, і без обмеження відповідь обривається
        # на півслові: «Зрозум», «`.\n\n2.». Перевірено 2026-08-15.
        return {"reasoning_effort": "none"}
    return {}


def _choose_model(text: str) -> str:
    """
    Яку модель питати першою.

    Раніше короткі команди йшли на «швидку llama-8b», але після переходу на
    gpt-oss і Gemini там опинилась gemini-3.1-flash-lite з найменшою квотою
    (500 на день, 15 за хвилину, і її ж їсть зір). Тепер усе йде на основну
    модель, резерв лише на помилку. Щоб повернути окрему модель для коротких
    команд, задай FAST_MODEL у config.json. Теми з _FORCE_PRIMARY туди не
    потрапляють ніколи.
    """
    t = text.lower()
    if (cfg.FAST_MODEL and not any(h in t for h in _FORCE_PRIMARY)
            and len(t.split()) <= 6 and any(h in t for h in _COMMAND_HINTS)):
        return cfg.FAST_MODEL
    return cfg.GROQ_PRIMARY_MODEL
