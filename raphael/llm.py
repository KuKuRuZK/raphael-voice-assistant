"""
Моделі: Groq, Gemini і Ollama за одним OpenAI-сумісним API, резервна
модель на будь-яку помилку, розбір тегів дій [ACTION:тип:параметр].
"""
import json
import logging
import os
import re
import time

from raphael import emotion
from raphael import runtime
from raphael import settings as cfg
from raphael import usage

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
    # Whisper пише і «плейліст»; «почати грати … спочатку» до 2026-11-03 йшло локальній
    "плейліст", "спочатку", "грати", "трек", "пісн",
    # Пошук: локальна gpt-oss-20b тут «вирішує» викликати функцію, якої в неї нема,
    # і повертає порожньо (3 з 4 спроб у перевірці 2026-11-03). Хай одразу хмара
    "знайди", "пошукай", "загугли", "погугли",
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

# Прохання до тіла на екрані (тег avatar:дія)
_BODY_HINTS = (
    "потанц", "танцю", "сядь", "визирни", "поспи", "постукай", "стукни", "перейди", "телепорт",
    "посмійся", "потягнись", "жуйк", "істерик", "повис", "фотобомб", "вилізь", "тицьни",
    "голограм", "курсор", "сховайся",
)


def _is_command(text: str) -> bool:
    """Команда, дорога тема чи прохання до тіла: тут потрібна дія з точним тегом."""
    t = text.lower()
    return any(h in t for h in _COMMAND_HINTS + _FORCE_PRIMARY + _BODY_HINTS)


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
    # Локальні моделі (OFFLINE_MODEL або навіть основна): local: це Ollama,
    # lmstudio: це сервер LM Studio (lms server start). Таймаут довший: перше
    # читання великого системного промту локальною моделлю повільне.
    "local":  {"base_url": "http://127.0.0.1:11434/v1",
               "key_name": "", "key_default": "ollama", "timeout": 60},
    "lmstudio": {"base_url": "http://127.0.0.1:1234/v1",
                 "key_name": "", "key_default": "lm-studio", "timeout": 60},
}
LOCAL_PROVIDERS = ("local:", "lmstudio:")
_llm_clients = {}


def _is_local(model: str) -> bool:
    return (model or "").startswith(LOCAL_PROVIDERS)


# ── Роздуми вголос ────────────────────────────────────────────────────────────
# Gemma 4 через Gemini API і частина локальних моделей пишуть міркування прямо
# у відповідь: <thought>...</thought> або <think>...</think>, англійською і на
# тисячі символів. Лін зачитала б їх уголос (знайдено на тесті 2026-10-12).
_THINK_OPEN = re.compile(r"<(think|thought|thinking|reasoning)>", re.I)
_THINK_BLOCK = re.compile(r"<(think|thought|thinking|reasoning)>.*?(</\1>|\Z)", re.I | re.S)
_THINK_TAGS = ("<think>", "<thought>", "<thinking>", "<reasoning>")


def strip_thinking(text):
    """Прибирає блоки міркувань; недописаний блок (обрізаний лімітом) теж."""
    if not text or "<" not in text:
        return text
    return _THINK_BLOCK.sub("", text).strip()


def _without_thinking(pieces):
    """Те саме для потоку: тег може бути розрізаний між шматками."""
    buf, closing = "", None
    for piece in pieces:
        buf += piece
        while buf:
            if closing:
                end = buf.lower().find(closing)
                if end < 0:
                    buf = buf[-(len(closing) - 1):]    # можливий початок закривального тегу
                    break
                buf, closing = buf[end + len(closing):].lstrip(), None
                continue
            m = _THINK_OPEN.search(buf)
            if m:
                if buf[:m.start()]:
                    yield buf[:m.start()]
                buf, closing = buf[m.end():], f"</{m.group(1).lower()}>"
                continue
            lt = buf.rfind("<")
            if lt >= 0 and any(t.startswith(buf[lt:].lower()) for t in _THINK_TAGS):
                if lt:
                    yield buf[:lt]
                buf = buf[lt:]                         # чекаємо, чи це не тег
            else:
                yield buf
                buf = ""
            break
    if buf and not closing:
        yield buf

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
        cli = OpenAI(api_key=key or pcfg.get("key_default", "none"), base_url=pcfg["base_url"],
                     timeout=pcfg.get("timeout", LLM_TIMEOUT), max_retries=0)
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
        # Помилка локальної моделі (Ollama не запущена) не має ховати, що
        # хмарні моделі вперлись у ліміт
        online = [e for m, e in self.errors if not _is_local(m)]
        return bool(online) and all(_is_rate_limit(e) for e in online)

    @property
    def minute_limited(self) -> bool:
        """Хоч одна хмарна модель уперлась у ліміт, який мине за кілька десятків секунд
        (а не денний): тоді чесно «спитай за хвилинку», а не «денний ліміт вичерпано»."""
        waits = [_retry_after(e) for m, e in self.errors if not _is_local(m)]
        return any(w is not None and w < 120 for w in waits)


def _is_rate_limit(e) -> bool:
    s = str(e).lower()
    return "429" in s or "rate_limit" in s or "rate limit" in s or "quota" in s


# Якщо після 429 чекати не довше цього, чекаємо і питаємо ту саму модель ще раз:
# це швидше й розумніше, ніж іти на слабший резерв
RATE_WAIT = 3.0


def _retry_after(e):
    """Скільки секунд провайдер просить почекати після 429. None: не ліміт або не сказав."""
    if not _is_rate_limit(e):
        return None
    headers = getattr(getattr(e, "response", None), "headers", None)
    try:
        if headers is not None and headers.get("retry-after"):
            return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))     # Groq пише це й у тексті
    return int(m.group(1) or 0) * 60 + float(m.group(2)) if m else None


def _patient(model: str, call):
    """call(model); на коротке 429 (до RATE_WAIT с) одна пауза і ще одна спроба."""
    try:
        return call(model)
    except Exception as e:
        wait = _retry_after(e)
        if wait is None or wait > RATE_WAIT:
            raise
        log.info(f"LLM {model}: ліміт за хвилину, чекаю {wait:.1f} с і питаю ще раз")
        time.sleep(wait + 0.2)
        return call(model)


def _fallback_for(model: str) -> str:
    """Резерв за замовчуванням: інша модель пари основна/резервна."""
    return cfg.GROQ_FALLBACK_MODEL if model == cfg.GROQ_PRIMARY_MODEL else cfg.GROQ_PRIMARY_MODEL


def _candidates(model: str, fallback) -> list:
    """
    Кого питати по черзі: модель, резерв на іншому провайдері і, якщо вона
    налаштована, локальна OFFLINE_MODEL, яка відповідає й без інтернету.
    fallback: None → інша модель пари основна/резервна; False → лише сама
    модель (зір: локальна модель зображень не бачить).
    """
    if fallback is not None and not fallback:
        return [model]
    out = [model]
    extra = (_fallback_for(model) if fallback is None else fallback, cfg.OFFLINE_MODEL)
    if _is_local(model) and model == cfg.BRAIN_LOCAL_MODEL:
        extra = (cfg.GROQ_PRIMARY_MODEL, cfg.GROQ_FALLBACK_MODEL)   # локальна не впоралась: обидві хмарні
    elif cfg.BRAIN_LOCAL_MODEL and not runtime.GAME:
        extra = (*extra, cfg.BRAIN_LOCAL_MODEL)                     # хмара мовчить (ліміт, 503): остання надія
    for m in extra:
        if m and m not in out:
            out.append(m)
    spare = cfg.GROQ_SPARE_MODEL                    # той самий Groq, але окремий ліміт за хвилину
    if spare and cfg.GROQ_PRIMARY_MODEL in out and spare not in out:
        out.insert(out.index(cfg.GROQ_PRIMARY_MODEL) + 1, spare)
    return out


def _next_model(errors: list, candidates: list) -> None:
    """Пише в лог, що модель не відповіла і кого питаємо далі (або що нікого)."""
    m, e = errors[-1]
    why = f"{type(e).__name__}: {str(e)[:150]}"
    if len(errors) < len(candidates):
        log.warning(f"LLM {m} не відповіла ({why}), пробую {candidates[len(errors)]}")
    elif len(errors) > 1:
        log.error(f"LLM резерв {m} теж не відповів: {why}")


def _usage_tokens(obj) -> int:
    """Скільки токенів порахував провайдер (usage у відповіді чи x_groq у потоці)."""
    u = getattr(obj, "usage", None)
    if u is None:
        extra = getattr(obj, "x_groq", None)
        u = extra.get("usage") if isinstance(extra, dict) else getattr(extra, "usage", None)
    if u is None:
        return 0
    n = u.get("total_tokens") if isinstance(u, dict) else getattr(u, "total_tokens", 0)
    return n if isinstance(n, int) else 0


# ── gpt-oss у LM Studio: свій шаблон harmony замість чату ─────────────────────
# Через /v1/chat/completions LM Studio сам розбирає вихід gpt-oss і падає, коли
# модель пише «<|channel|>final <|constrain|>[EMO:sly] …» без «<|message|>»: тег
# настрою на початку відповіді вона приймає за формат обмеження. Це 400 «does not
# match the expected peg-native format» і порожні відповіді, разом ≈1 з 4 запитів
# 2026-11-03. Через /v1/completions з тим самим шаблоном і своїм розбором: 0 з 30.

def _is_harmony(model: str) -> bool:
    return model.startswith("lmstudio:") and model.split(":", 1)[1].startswith("openai/gpt-oss")


def _text_of(content) -> str:
    if isinstance(content, list):                    # [{"type": "text", ...}, картинки]
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _harmony_prompt(messages) -> str:
    out = ["<|start|>system<|message|>You are ChatGPT, a large language model trained by OpenAI.\n"
           # Без commentary: це канал викликів функцій, а функцій у Лін нема, дії йдуть тегами
           "Reasoning: low\n\n# Valid channels: analysis, final. "
           "Channel must be included for every message.<|end|>"]
    for m in messages:
        text = _text_of(m.get("content"))
        if m.get("role") == "system":
            out.append(f"<|start|>developer<|message|># Instructions\n\n{text}<|end|>")
        elif m.get("role") == "assistant":
            out.append(f"<|start|>assistant<|channel|>final<|message|>{text}<|end|>")
        else:
            out.append(f"<|start|>user<|message|>{text}<|end|>")
    return "".join(out) + "<|start|>assistant"


def _harmony_final(raw: str) -> str:
    """Відповідь з каналу final; роздуми (analysis) і службові токени відкидаємо.
    Без final модель інколи «викликає функцію» каналом commentary:
    «<|channel|>commentary to=assistant <|constrain|>… <|message|>{"name":"get_time"}»
    (перевірка 2026-11-03); тоді це дія, і ми робимо з неї тег.
    Нема ні того, ні того (ліміт зʼїли роздуми) → "", і відповідає хмара."""
    i = raw.rfind("<|channel|>final")
    if i < 0:
        return _harmony_commentary(raw)
    text = raw[i + len("<|channel|>final"):]
    if "<|message|>" in text:                        # «<|constrain|>EMO:sly<|message|>текст»
        text = text.split("<|message|>", 1)[1]
    text = re.sub(r"<\|[a-z]+\|>", " ", text)
    return re.sub(r"^\s*(?:json|text)\b", "", text).strip()


def _harmony_commentary(raw: str) -> str:
    i = raw.rfind("<|channel|>commentary")
    if i < 0 or "<|message|>" not in raw[i:]:
        return ""
    text = re.sub(r"<\|[a-z]+\|>", " ", raw[i:].split("<|message|>", 1)[1]).strip()
    try:
        call = json.loads(text)
    except ValueError:
        return text                                  # звичайний текст не в тому каналі
    if not isinstance(call, dict) or not call.get("name"):
        return ""
    args = call.get("arguments") or call.get("params") or call.get("parameters") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"param": args}
    param = next(iter(args.values()), "") if isinstance(args, dict) and args else ""
    return f"[ACTION:{call['name']}:{param}]"


def _harmony_call(cli, bare: str, messages, **kw):
    resp = cli.completions.create(
        model=bare, prompt=_harmony_prompt(messages), stop=["<|return|>", "<|call|>"],
        max_tokens=kw.get("max_tokens") or 450, temperature=kw.get("temperature", 0.7))
    raw = (resp.choices[0].text if resp.choices else "") or ""
    from types import SimpleNamespace
    msg = SimpleNamespace(content=_harmony_final(raw), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=resp.choices[0].finish_reason
                                                    if resp.choices else "stop")],
                           usage=getattr(resp, "usage", None))


def _llm_call(model: str, messages, **kw):
    cli, bare = _llm(model)
    if _is_local(model):
        kw.pop("tools", None)             # локальні моделі функцій часто не вміють
    if _is_harmony(model):
        resp = _harmony_call(cli, bare, messages, **kw)
        usage.record(model, tokens=_usage_tokens(resp))
        return resp
    # Інші моделі LM Studio теж можуть ламати формат так само: два повтори
    for attempt in range(3):
        try:
            resp = cli.chat.completions.create(model=bare, messages=messages,
                                               **_model_kwargs(bare), **kw)
            break
        except Exception as e:
            if attempt == 2 or not (_is_local(model) and "does not match the expected" in str(e)):
                raise
            log.info(f"LLM {model}: зламаний формат відповіді, питаю ще раз")
    usage.record(model, tokens=_usage_tokens(resp))
    for choice in getattr(resp, "choices", None) or []:
        msg = getattr(choice, "message", None)
        content = getattr(msg, "content", None)
        if content and "<" in content:
            msg.content = strip_thinking(content)
    return resp


def llm_chat(model: str, messages, fallback=None, **kw):
    """
    Єдина точка виклику моделі. Сама підставляє параметри під провайдера.

    Якщо модель не відповіла з БУДЬ-ЯКОЇ причини (429, таймаут, 5xx, обрив
    мережі, модель прибрали), питаємо резервну на іншому провайдері, а далі
    локальну (див. _candidates).
    fallback: None → автоматично інша модель пари основна/резервна;
              False → без резерву (зір: на Groq моделей із зображеннями немає).
    Якщо не відповіла жодна, кидає LLMUnavailable.
    """
    candidates, errors = _candidates(model, fallback), []
    for m in candidates:
        try:
            resp = _patient(m, lambda m: _llm_call(m, messages, **kw))
            if _is_local(m) and m is not candidates[-1]:
                _check_local_reply(resp)
            try:
                resp.lin_model = m                   # хто відповів насправді (assistant чистить теги локальної)
            except Exception:
                pass
            return resp
        except Exception as e:
            errors.append((m, e))
            _next_model(errors, candidates)
    raise LLMUnavailable(errors) from errors[-1][1]


# Літери, яких нема в українській: локальна модель зіслизнула на російську
_RUSSIAN = re.compile(r"[ыэъёЫЭЪЁ]")


class BadLocalReply(Exception):
    """Локальна модель відповіла, але так, що краще спитати хмару."""


# Дії, які локальна модель може додати сама до звичайної розмови: лише ті, що щось
# читають. Команди (увімкни, відкрий, потанцюй…) і так ідуть у хмару (_is_command), тож
# інша справжня дія від локальної означає, що команду не впізнали: тоді питаємо хмару.
# Теги тіла й вигадані теги просто прибираємо: Gemma 26B чіпляла тег тіла майже до
# кожної відповіді, а на «поясни, що таке чорна діра» ще й web_search (2026-11-03).
_LOCAL_TALK_ACTIONS = (
    "get_time", "weather", "calendar_today", "calendar_tomorrow", "calendar_week",
    "gmail_unread", "gmail_latest", "slack_unread", "spotify_current", "system_info",
    "note_list", "diary_find", "usage_report",
)


def _tag_name(m) -> str:
    name, param = m.group(1).lower(), m.group(2)
    return param.lower() if name == "action" and ":" not in param else name


def drop_unasked_tags(reply: str) -> str:
    """Прибрати теги тіла, вигадані теги ([ACTION:peek], [dance]) і тег, обірваний лімітом."""
    def keep(m):
        name = _tag_name(m)
        return " " if name == "avatar" or not _plausible_action(name) else m.group(0)
    out = _ACTION_RE.sub(keep, reply or "")
    out = _BRACKET_NAME_RE.sub(lambda m: m.group(0) if _plausible_action(m.group(1).lower()) else " ", out)
    out = re.sub(r"\[[^\]\[]*$", "", out)
    return re.sub(r"[ 	]{2,}", " ", out).strip()


def _unasked_action(text: str) -> str:
    """Справжня дія, яку локальна не мала б пропонувати на розмову, або ""."""
    for m in _ACTION_RE.finditer(text):
        if _tag_name(m) not in _LOCAL_TALK_ACTIONS:
            return _tag_name(m)
    for m in _BRACKET_NAME_RE.finditer(text):
        if m.group(1).lower() not in _LOCAL_TALK_ACTIONS:
            return m.group(1).lower()
    m = _BARE_ACTION_RE.match(text)
    if m and _plausible_action(m.group(1).lower()) and m.group(1).lower() not in _LOCAL_TALK_ACTIONS:
        return m.group(1).lower()
    return ""


def _check_local_reply(resp) -> None:
    """Невелика локальна модель (gpt-oss-20b) інколи: віддає порожньо (роздуми
    з'їли ліміт) або переходить на російську. Тоді відповідає наступна (хмара)."""
    choice = (getattr(resp, "choices", None) or [None])[0]
    msg = getattr(choice, "message", None)
    if getattr(msg, "tool_calls", None):
        return
    text = emotion.EMO_RE.sub("", getattr(msg, "content", None) or "").strip()   # тег настрою це не текст і не дія
    # Теги тіла й вигадані assistant однаково прибере (drop_unasked_tags): через них
    # відповідь не бракуємо, інакше Gemma з «[ACTION:avatar:stretch]» щоразу йшла б у хмару
    text = drop_unasked_tags(text)
    if not text:
        raise BadLocalReply("порожня відповідь")
    unasked = _unasked_action(text)
    if unasked:
        raise BadLocalReply(f"дія без прохання ({unasked}): {text[:60]!r}")
    if _RUSSIAN.search(text):
        raise BadLocalReply(f"російська: {text[:60]!r}")
    # Теги дій: невідомий тип ([ACTION:app:open]), голий тег посеред тексту чи поруч
    # із тегом у дужках (його зачитали б уголос), або дужки без дії («[dance]»)
    # «[ACTION:get_time]» без параметра теж справжня дія (_find_action її розуміє)
    tags = [(p, "") if t.lower() == "action" and ":" not in p else (t, p) for t, p in _ACTION_RE.findall(text)]
    if any(not _plausible_action(t.lower()) for t, _ in tags):
        raise BadLocalReply(f"невідома дія: {text[:60]!r}")
    rest = _ACTION_RE.sub("", text)
    bare = _BARE_ANYWHERE.search(rest)
    if bare and (tags or bare.start() > 0 or not _plausible_action(bare.group(1).lower())):
        raise BadLocalReply(f"голий тег у тексті: {text[:60]!r}")
    if re.search(r"\[[^\]]*\]", rest):
        raise BadLocalReply(f"дужки без дії: {text[:60]!r}")


def _plausible_action(name: str) -> bool:
    return name.startswith(_ACTION_PREFIXES) or name in ("weather", "timer", "hotkey", "screenshot")


# Голий тег будь-де в тексті (для перевірки локальної відповіді)
_BARE_ANYWHERE = re.compile(r"(?<![\w\[])([a-z][a-z_]{2,24})\s*:\s*\S", re.I)


def _stream_text(stream, model: str = ""):
    """Текстові шматки з потоку OpenAI chat.completions (міркування пропускаємо)."""
    for chunk in stream:
        tokens = _usage_tokens(chunk)     # приходить в останньому шматку, якщо взагалі
        if tokens and model:
            usage.record(model, requests=0, tokens=tokens)
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
        if _is_harmony(m):
            # Потоком через свій шаблон не вийде (final видно лише в кінці),
            # тож одним шматком: локальна відповідає за 1-3 с
            text = _llm_call(m, messages, **kw).choices[0].message.content
            if not text:
                raise BadLocalReply("порожня відповідь")
            return text, iter(())
        cli, bare = _llm(m)
        stream = cli.chat.completions.create(
            model=bare, messages=messages, stream=True, **_model_kwargs(bare), **kw)
        usage.record(m)
        pieces = _without_thinking(_stream_text(stream, m))
        return next(pieces, ""), pieces   # чекаємо перший шматок тексту

    candidates, errors = _candidates(model, fallback), []
    for m in candidates:
        try:
            first, rest = _patient(m, _open)
            break
        except Exception as e:
            errors.append((m, e))
            _next_model(errors, candidates)
    else:
        raise LLMUnavailable(errors) from errors[-1][1]
    if first:
        yield first
    yield from rest


# ── Tool calling (TOOL_CALLING, типово вимкнено) ─────────────────────────────
# Одна функція на всі дії, а не сотня окремих: перелік дій і формат параметрів
# уже є в системному промпті, і сотня схем зʼїла б денний ліміт токенів.
ACTION_TOOL = {"type": "function", "function": {
    "name": "do_action",
    "description": "Виконати одну дію з переліку ДІЇ в системних інструкціях.",
    "parameters": {"type": "object", "properties": {
        "type":  {"type": "string", "description": "назва дії, напр. spotify_play"},
        "param": {"type": "string",
                  "description": "параметр, як після двокрапки в тегі; порожній, якщо не потрібен"},
    }, "required": ["type"]},
}}


def tool_kwargs() -> dict:
    return {"tools": [ACTION_TOOL]} if cfg.TOOL_CALLING else {}


def _tool_action(message) -> str:
    """
    Перший виклик do_action з відповіді моделі у вигляді тегу
    [ACTION:тип:параметр], або "". Далі дія йде тим самим шляхом, що й тег
    у тексті: ті самі перевірки небезпечних дій і підтвердження.
    """
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        if getattr(fn, "name", "") != "do_action":
            continue
        try:
            args = json.loads(getattr(fn, "arguments", "") or "{}")
        except ValueError:
            log.warning(f"do_action: не JSON у параметрах: {getattr(fn, 'arguments', '')!r}")
            continue
        action = str(args.get("type") or "").strip().lower()
        if re.fullmatch(r"[a-z_]+", action):
            param = str(args.get("param") or "").replace("]", ")").strip()
            return f"[ACTION:{action}:{param}]"
    return ""


# Розбір тега дії. Терпимий до пробілів і регістру: gpt-oss інколи пише
# "[ ACTION:spotify_pause: ]", і сувора версія такий тег просто не бачила.
# Тег настрою [EMO:happy] під цей вигляд теж підпадав би, але це не дія (raphael.emotion)
_ACTION_RE = re.compile(r"\[\s*(?:ACTION\s*:\s*)?(?!EMO(?:TION)?\s*:)([A-Za-z_]+)\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)

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
    "vibe_", "avatar", "usage_", "diary_",
)

# Дія в дужках, але без двокрапки: «[gmail_unread]»
_BRACKET_NAME_RE = re.compile(r"\[\s*([a-z][a-z_]{2,24})\s*\]", re.I)


def _real_action(name: str, param: str) -> bool:
    """Відомий тип дії, а для тіла ще й відома дія тіла. gpt-oss дописує вигадане
    «[ACTION:avatar:none]» до зовсім іншої команди («spotify_shuffle:on [ACTION:avatar:none]»),
    і вигаданий тег у дужках перебивав справжню дію (перевірка 2026-11-03)."""
    if not _plausible_action(name):
        return False
    if name == "avatar":
        from raphael import avatar                   # avatar сам тягне llm
        return param in avatar.BODY
    return True


def _find_action(reply: str):
    """Шукає дію: голу на самому початку, потім у дужках. → (тип, параметр, чи_в_дужках).
    Гола на початку перша, бо так модель і пише справжню дію, а в дужки слідом дописує
    зайве: «dictate_last:[ACTION:avatar:type]», «system_info:[ACTION:system_health]»,
    «note_remind:мама|21:00 [ACTION:note_remind]» (перевірка 2026-11-03, gpt-oss-20b).
    Вигадані теги пропускає й шукає далі."""
    # Тег настрою стає межею, а не зникає: з «weather:[EMO:tease] Погода? Дивимось.»
    # інакше виходило місто «Погода? Дивимось.» (лог 2026-11-03, 11:12)
    reply = emotion.EMO_RE.sub("\n", reply or "").strip()  # «[EMO:happy] note_remind:…»: голий тег після настрою
    m = _BARE_ACTION_RE.match(reply)
    if m:
        name = m.group(1).lower()
        # Параметр до першого тегу чи кінця рядка: далі вже звичайна мова
        # (\s* у _BARE_ACTION_RE ковтає перенос, тому рядок беремо від двокрапки)
        rest = reply[reply.index(":", m.start(1)) + 1:].lstrip(" \t")
        param = re.split(r"\s*\[|\n", rest, maxsplit=1)[0].strip().rstrip(";,")
        if (name.startswith(_ACTION_PREFIXES) or name in ("weather", "timer", "hotkey")) and (
                name != "avatar" or _real_action(name, param)):
            log.info(f"Дія без дужок, приймаю: {name}")
            return name, param, False
    found = []
    for m in _ACTION_RE.finditer(reply):
        name, param = m.group(1).lower(), m.group(2).strip()
        if name == "action" and ":" not in param:          # «[ACTION:slack_unread]»: без параметра
            name, param = param.lower(), ""
        if _real_action(name, param):
            found.append((name, param))
    if found:
        # Тіло лише прикраса, коли поруч справжня дія: на «пиши» gpt-oss-120b дала
        # «[ACTION:avatar:type] [ACTION:dictate_type:]» (перевірка 2026-11-03)
        name, param = next((f for f in found if f[0] != "avatar"), found[0])
        return name, param, True
    for m in _BRACKET_NAME_RE.finditer(reply):
        name = m.group(1).lower()
        if name != "avatar" and _real_action(name, ""):
            return name, "", True
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
    if m.startswith("google/gemma-4"):
        # Gemma 4 в LM Studio: без цього думає 20-90 с і вичерпує ліміт токенів
        # з порожньою відповіддю; з "none" відповідь за ≈1 с. enable_thinking
        # і "low" вона ігнорує (перевірено 2026-11-02, LM Studio 0.4).
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
    model = main_model()
    # Локальна (gpt-oss-20b) теги дій часто ламає: «[ACTION:app:open]», голий тег
    # поряд із тегом у дужках, не та дія. Команди одразу в хмару, розмова локально.
    if _is_local(model) and _is_command(t):
        return cfg.GROQ_PRIMARY_MODEL
    return model


def main_model() -> str:
    """Основна модель зараз: локальна (BRAIN_LOCAL_MODEL), поки не граєш; у грі хмарна."""
    if cfg.BRAIN_LOCAL_MODEL and not runtime.GAME:
        return cfg.BRAIN_LOCAL_MODEL
    return cfg.GROQ_PRIMARY_MODEL


# ── Локальна модель: вивантажити на гру, завантажити після ──────────────────
def _lms(*args, timeout=180) -> bool:
    import subprocess
    if not os.path.exists(cfg.LMS_EXE):
        log.warning(f"Мозок: нема {cfg.LMS_EXE}, LM Studio не керую")
        return False
    r = subprocess.run([cfg.LMS_EXE, *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        log.warning(f"Мозок: lms {' '.join(args)}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.returncode == 0


def _ollama(payload: dict) -> bool:
    import urllib.request
    base = LLM_PROVIDERS["local"]["base_url"].rsplit("/v1", 1)[0]
    req = urllib.request.Request(base + "/api/generate", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status == 200


def local_unload() -> bool:
    """Звільнити відеопамʼять під гру: вивантажити локальну модель."""
    m = cfg.BRAIN_LOCAL_MODEL
    if m.startswith("lmstudio:"):
        ok = _lms("unload", "--all")
    elif m.startswith("local:"):
        ok = _ollama({"model": m.split(":", 1)[1], "keep_alive": 0})
    else:
        return False
    log.info(f"Мозок: {m} {'вивантажено' if ok else 'не вивантажено'} на час гри")
    return ok


def _lms_load_lean(name: str) -> bool:
    """Завантажити модель у LM Studio без копії ваг в оперативці.

    За замовчуванням LM Studio тримає ваги ще й у RAM (keepModelInMemory, mmap), навіть
    коли модель на відеокарті: gpt-oss-20b займала 12-14 ГБ оперативки, і з голосом та
    іграми памʼять доходила до 98 %. Без цього та сама модель забирає ≈4 ГБ (заміри
    2026-11-03: зайнято 25 → 15 ГБ), а Gemma 26B вміщається з 18 ГБ. Ці налаштування
    є лише в SDK (pip lmstudio), `lms load` їх не має. Нема SDK → False, і вантажить CLI."""
    try:
        import lmstudio
        from lmstudio import LlmLoadModelConfig
    except ImportError:
        return False
    try:
        with lmstudio.Client() as client:
            # ttl=None: інакше SDK вивантажує модель за годину простою, а наступний
            # запит LM Studio завантажує сам, уже з копією в оперативці
            client.llm.load_new_instance(name, ttl=None, config=LlmLoadModelConfig(
                gpu={"ratio": 1.0}, context_length=cfg.BRAIN_LOCAL_CONTEXT,
                keep_model_in_memory=False, try_mmap=False))
        return True
    except Exception as e:
        log.warning(f"Мозок: економне завантаження не вдалось ({e}), вантажу як звичайно")
        return False


def local_preload() -> bool:
    """Завантажити локальну модель заздалегідь, щоб перша відповідь не чекала."""
    m = cfg.BRAIN_LOCAL_MODEL
    if m.startswith("lmstudio:"):
        name = m.split(":", 1)[1]
        import subprocess
        try:
            ps = subprocess.run([cfg.LMS_EXE, "ps"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except Exception:
            ps = ""
        if name in ps:
            return True                              # уже в памʼяті: друга копія не потрібна
        # Один слот: Лін питає по одному, а кожен зайвий слот тримає свій кеш контексту (≈0,3 ГБ)
        ok = _lms("server", "start", timeout=60) and (_lms_load_lean(name) or _lms(
            "load", name, "-c", str(cfg.BRAIN_LOCAL_CONTEXT), "--gpu", "max", "--parallel", "1", "-y", timeout=300))
    elif m.startswith("local:"):
        ok = _ollama({"model": m.split(":", 1)[1], "keep_alive": "30m"})
    else:
        return False
    log.info(f"Мозок: {m} {'завантажено' if ok else 'не завантажилась'}")
    if ok:
        _warm_up(m)
    return ok


def _warm_up(model: str) -> None:
    """Прогнати системний промт: перше читання 11 тис. символів займає ≈8 с, далі
    він у кеші моделі, і перша справжня відповідь приходить за ≈1,5 с."""
    from raphael import prompt                       # prompt сам тягне llm через vibes
    try:
        _llm_call(model, prompt.inject_time([{"role": "system", "content": prompt.build_system_prompt()},
                                             {"role": "user", "content": "привіт"}]), max_tokens=1)
    except Exception as e:
        log.debug(f"Мозок: розігрів не вдався: {e}")
