"""
Лічильник денних лімітів: скільки запитів, токенів і секунд аудіо пішло
сьогодні на кожну модель. Безкоштовні ліміти Groq і Gemini денні, і коли
вони закінчувались, Рафаель мовчки переходив на резерв, а потім казав лише
«ліміт». Тепер на 80% ліміту він попереджає (раз на день), а на питання
«скільки лімітів» каже, скільки вже витрачено.

Рахунок локальний: запити з того самого ключа з інших програм сюди не
потрапляють, а провайдери обнуляють ліміти не опівночі за місцевим часом.
Тому це оцінка, і Рафаель так і каже: «за моїм підрахунком».
"""
import json
import logging
import os
import threading
from datetime import datetime

from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")

_lock = threading.Lock()
_state = None   # {"date", "counts": {ключ: {"requests", "tokens", "seconds"}}, "warned": [...]}
_KINDS = {"requests": ("запит", "запити", "запитів"),
          "tokens": ("токен", "токени", "токенів"),
          "seconds": ("секунда аудіо", "секунди аудіо", "секунд аудіо")}


def key(model: str) -> str:
    """Ключ лічильника «провайдер:модель»; без префікса модель на Groq."""
    prov, sep, bare = (model or "").partition(":")
    return model if sep and prov in ("groq", "gemini", "local") else f"groq:{model}"


def _load() -> dict:
    global _state
    today = datetime.now().strftime("%Y-%m-%d")
    if _state is None:
        try:
            with open(cfg.USAGE_PATH, encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
    if _state.get("date") != today:
        _state = {"date": today, "counts": {}, "warned": []}
    return _state


def _save(state: dict) -> None:
    try:
        tmp = cfg.USAGE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, cfg.USAGE_PATH)
    except OSError as e:
        log.debug(f"usage.json: {e}")


def _name(k: str) -> str:
    """Як назвати модель уголос."""
    if k == key(cfg.GROQ_PRIMARY_MODEL):
        return "основна модель"
    if k == key(cfg.GROQ_FALLBACK_MODEL):
        # Зір зазвичай на тій самій моделі, тож і ліміт у них спільний
        return "резервна модель і зір" if k == key(cfg.VISION_MODEL) else "резервна модель"
    if k == key(cfg.VISION_MODEL):
        return "зір"
    if "whisper" in k:
        return "розпізнавання мови"
    return k.split(":", 1)[-1]


def _warning(k: str, used: dict, state: dict) -> str:
    """Текст попередження, якщо щойно перетнули 80% або 100% якогось ліміту."""
    limits = cfg.DAILY_LIMITS.get(k) or {}
    for kind, limit in limits.items():
        if not limit or kind not in used:
            continue
        share = used[kind] / limit
        level = 100 if share >= 1 else 80 if share >= 0.8 else 0
        mark = f"{k}|{kind}|{level}"
        if not level or mark in state["warned"]:
            continue
        state["warned"] += [mark, f"{k}|{kind}|80"]      # після 100% про 80% вже не кажемо
        what = _KINDS[kind][2]
        if level == 100:
            return f"{_name(k)}: денний ліміт {what} вичерпано, за моїм підрахунком. Далі працюватиме резерв."
        return f"{_name(k)}: витрачено вже {round(share * 100)}% денного ліміту {what}."
    return ""


def record(model: str, requests: int = 1, tokens: int = 0, seconds: float = 0) -> None:
    """Успішний запит до моделі (і скільки токенів чи секунд аудіо, якщо відомо)."""
    k = key(model)
    with _lock:
        state = _load()
        used = state["counts"].setdefault(k, {"requests": 0, "tokens": 0, "seconds": 0})
        used["requests"] += requests
        used["tokens"] += int(tokens or 0)
        used["seconds"] = round(used.get("seconds", 0) + (seconds or 0), 1)
        warning = _warning(k, used, state)
        _save(state)
    if warning:
        log.warning(warning)
        _announce(warning)


def _announce(text: str) -> None:
    # Окремим потоком: запит до моделі не має чекати, поки це прозвучить
    threading.Thread(target=tts.speak, args=(text,), daemon=True).start()


def _amount(n: float) -> str:
    n = round(n)
    return vr.count(round(n / 1000), "тисяча", "тисячі", "тисяч") if n >= 10_000 else str(n)


def report_text() -> str:
    """Скільки витрачено сьогодні, для відповіді голосом."""
    with _lock:
        counts = {k: dict(v) for k, v in _load()["counts"].items()}
    if not counts:
        return "Сьогодні я ще не зверталась до моделей."
    parts = []
    for k, used in sorted(counts.items(), key=lambda kv: -kv[1]["requests"]):
        limits = cfg.DAILY_LIMITS.get(k) or {}
        bits = []
        for kind, forms in _KINDS.items():
            n = used.get(kind, 0)
            if not n and kind != "requests":
                continue
            if kind == "seconds":
                text = vr.count(max(1, round(n / 60)), "хвилина аудіо", "хвилини аудіо", "хвилин аудіо")
                if limits.get(kind):
                    text += f" з {round(limits[kind] / 60)}"
            else:
                text = (f"{_amount(n)} {vr.plural(round(n), *forms)}" if n < 10_000
                        else f"приблизно {_amount(n)} {forms[2]}")
                if limits.get(kind):
                    text += f" з {_amount(limits[kind])}"
            bits.append(text)
        parts.append(f"{_name(k)}: " + ", ".join(bits))
    return "За моїм підрахунком сьогодні: " + "; ".join(parts) + "."
