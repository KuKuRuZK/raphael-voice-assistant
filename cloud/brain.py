"""
Серверна Лін: відповідає в Telegram, коли ПК Влада вимкнений.

Хмарні моделі через OpenAI-сумісний API (Groq, Gemini), по черзі до першої, що відповіла.
Промт короткий: характер з persona.txt і кілька дій, яким не потрібен ПК. Домашній промт
≈4,5 тисячі токенів, і на безкоштовних 8 тисячах токенів за хвилину Groq це менше двох
відповідей на хвилину (з телефона 2026-11-07 третє голосове підряд отримало 429).
"""
import json
import logging
import os
import re

from raphael import emotion
from raphael import settings as cfg

from cloud import tools

log = logging.getLogger("Лін")

DATA = os.environ.get("LIN_DATA") or os.path.join(cfg._ROOT, "data")
PROVIDERS = {
    "groq":   ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
}
MODELS = [m.strip() for m in (os.environ.get("LIN_CLOUD_MODELS") or
          "groq:openai/gpt-oss-120b,gemini:gemini-3.1-flash-lite,groq:openai/gpt-oss-20b").split(",") if m.strip()]
HISTORY = 12                # останні репліки, що йдуть у запит (6 обмінів)
ACTION_RE = re.compile(r"\[ACTION:([a-z_]+):([^\]]*)\]", re.I)

_clients = {}


class Unavailable(Exception):
    """Жодна модель не відповіла."""


def _client(provider: str):
    if provider not in _clients:
        from openai import OpenAI
        url, key = PROVIDERS[provider]
        _clients[provider] = OpenAI(base_url=url, api_key=cfg._secret(key) or "нема", timeout=30, max_retries=0)
    return _clients[provider]


def _kwargs(model: str) -> dict:
    """Міркувальні моделі без обмеження думають довго й обривають відповідь (як і вдома)."""
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    if "gemini" in model:
        return {"reasoning_effort": "none"}
    return {}


def chat(messages: list, max_tokens: int = 600) -> str:
    """Перша модель з MODELS, що відповіла непорожнім текстом."""
    errors = []
    for spec in MODELS:
        provider, _, model = spec.partition(":")
        try:
            r = _client(provider).chat.completions.create(model=model, messages=messages, max_tokens=max_tokens,
                                                          temperature=0.7, **_kwargs(model))
            text = (r.choices[0].message.content or "").strip()
            if text:
                return text
            errors.append(f"{spec}: порожньо")
        except Exception as e:
            errors.append(f"{spec}: {type(e).__name__}: {str(e)[:120]}")
    log.warning("Моделі не відповіли: " + "; ".join(errors))
    raise Unavailable("; ".join(errors))


def transcribe(data: bytes, seconds: float = 0) -> str:
    """Голосове → текст (Groq Whisper)."""
    r = _client("groq").audio.transcriptions.create(file=("voice.ogg", data), model="whisper-large-v3-turbo",
                                                    language="uk", response_format="text", temperature=0.0)
    return (r if isinstance(r, str) else getattr(r, "text", "") or "").strip()


def persona() -> str:
    """Характер з persona.txt (у теці даних на сервері); рядки з # пропускаються."""
    for path in (os.path.join(DATA, "persona.txt"), os.path.join(cfg._ROOT, "persona.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                text = " ".join(l.strip() for l in f if l.strip() and not l.lstrip().startswith("#"))
            if text:
                return text
        except FileNotFoundError:
            continue
    return "Ти Лін, жива й трохи зухвала помічниця Влада. Говориш українською, коротко й по-людськи."


def system_prompt() -> str:
    return (
        persona() + " "
        "ЗАРАЗ: ти на сервері в хмарі, бо компʼютер Влада вимкнений. Влад пише тобі в Telegram з телефона. "
        "Голосу, тіла, музики, пошти, календаря, файлів і екрана зараз нема: якщо просить таке, чесно скажи, "
        "що комп вимкнений, і запропонуй нагадати, коли він буде за компом. "
        "Відповідай коротко, як у чаті: одне-три речення. Не вигадуй фактів: не знаєш, то шукай або кажи, що не знаєш. "
        "ДІЇ: один тег у кінці відповіді, коли треба: "
        "[ACTION:weather:місто] погода зараз і на завтра (порожньо: Вільнюс); "
        "[ACTION:search:запит] пошук в інтернеті, коли питають факти, новини, ціни, розклади; "
        "[ACTION:remind:час|текст] нагадати в Telegram, час: хвилини '30', '18:00', 'завтра 09:00'. "
        "Приклади: 'яка завтра погода' → [ACTION:weather:]; 'нагадай через годину подзвонити мамі' → "
        "[ACTION:remind:60|подзвонити мамі]; 'хто виграв вчора' → [ACTION:search:хто виграв вчора]. "
        f"Зараз {tools.now():%H:%M, %d.%m.%Y} (Вільнюс)."
    )


class Brain:
    """Розмова серверної Лін: історія, дії, відповідь текстом."""

    def __init__(self, reminders: "tools.Reminders" = None, history_path: str = None):
        self.reminders = reminders or tools.Reminders(os.path.join(DATA, "reminders.json"))
        self.history_path = history_path or os.path.join(DATA, "history.json")
        self.history = self._load()

    def _load(self) -> list:
        try:
            with open(self.history_path, encoding="utf-8") as f:
                return json.load(f)[-HISTORY:]
        except (FileNotFoundError, ValueError):
            return []

    def _remember(self, user: str, reply: str) -> None:
        self.history = (self.history + [{"role": "user", "content": user},
                                        {"role": "assistant", "content": reply}])[-HISTORY:]
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except OSError as e:
            log.warning(f"Історію не збережено: {e}")

    def _act(self, kind: str, param: str) -> tuple:
        """→ (результат дії, чи треба ще раз спитати модель з ним)."""
        kind = kind.lower()
        if kind == "weather":
            return tools.weather(param), True
        if kind == "search":
            return tools.search(param), True
        if kind == "remind":
            return self.reminders.add(param), False
        return "", False

    def answer(self, text: str) -> str:
        messages = [{"role": "system", "content": system_prompt()}, *self.history, {"role": "user", "content": text}]
        try:
            reply = chat(messages)
            m = ACTION_RE.search(reply)
            spoken = emotion.take(ACTION_RE.sub("", reply))[1]
            if m:
                result, again = self._act(m.group(1), m.group(2).strip())
                log.info(f"Сервер: дія {m.group(1)}:{m.group(2)[:60]}")
                if again and result:
                    # Модель бачить результат і відповідає вже з ним, своїми словами
                    reply = chat(messages + [{"role": "assistant", "content": reply},
                                             {"role": "user", "content": f"Результат ({m.group(1)}):\n{result}\n"
                                              "Тепер відповідай Владу коротко, без тегів."}])
                    spoken = emotion.take(ACTION_RE.sub("", reply))[1]
                elif result:
                    spoken = f"{spoken} {result}".strip() if spoken and result not in spoken else result
        except Unavailable:
            return "Хмарні моделі зараз не відповідають (ліміт або мережа). Спробуй за хвилину."
        except Exception as e:
            log.error(f"Сервер: відповідь не вдалась: {e}", exc_info=True)
            return "Щось зламалось у мене на сервері, деталі в лозі."
        spoken = spoken or "Хм, загубила думку. Повтори?"
        self._remember(text, spoken)
        return spoken
