"""Дії серверної Лін: погода, пошук в інтернеті, нагадування в Telegram."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta

import httpx

log = logging.getLogger("Лін")

_tz = []


def now() -> datetime:
    """Час у Вільнюсі: сервер живе в UTC. На Windows без пакета tzdata поясів нема,
    тоді місцевий час машини (там це той самий Вільнюс)."""
    if not _tz:
        try:
            from zoneinfo import ZoneInfo
            _tz.append(ZoneInfo(os.environ.get("LIN_TZ", "Europe/Vilnius")))
        except Exception:
            _tz.append(datetime.now().astimezone().tzinfo)
    return datetime.now(_tz[0])


# ── Погода (Open-Meteo, без ключа) ───────────────────────────────────────────

_WMO = {0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно", 45: "туман", 48: "туман з інеєм",
        51: "мряка", 53: "мряка", 55: "сильна мряка", 61: "невеликий дощ", 63: "дощ", 65: "сильний дощ",
        66: "крижаний дощ", 67: "крижаний дощ", 71: "невеликий сніг", 73: "сніг", 75: "сильний сніг",
        77: "снігова крупа", 80: "злива", 81: "злива", 82: "сильна злива", 85: "снігопад", 86: "сильний снігопад",
        95: "гроза", 96: "гроза з градом", 99: "гроза з градом"}


def weather(city: str) -> str:
    """Погода зараз і на завтра, коротким текстом для моделі (вона перекаже по-своєму)."""
    city = (city or "").strip() or os.environ.get("LIN_CITY", "Vilnius")
    found = httpx.get("https://geocoding-api.open-meteo.com/v1/search", timeout=10,
                      params={"count": 1, "language": "uk", "name": city}).json().get("results") or []
    if not found:
        return f"Місто «{city}» не знайдено."
    place = found[0]
    data = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}).json()
    cur, day = data["current"], data["daily"]
    return (f"{place.get('name', city)} зараз: {_WMO.get(cur.get('weather_code'), 'погода')}, "
            f"{round(cur['temperature_2m'])}°, відчувається як {round(cur['apparent_temperature'])}°, "
            f"вітер {round(cur['wind_speed_10m'])} км/г. Завтра: {_WMO.get(day['weather_code'][1], '')}, "
            f"від {round(day['temperature_2m_min'][1])} до {round(day['temperature_2m_max'][1])}°, "
            f"дощ {int(day['precipitation_probability_max'][1] or 0)}%.")


# ── Пошук ────────────────────────────────────────────────────────────────────

def search(query: str, limit: int = 5) -> str:
    """Короткі результати пошуку (DuckDuckGo через ddgs) для моделі, або чесне «не вийшло»."""
    try:
        from ddgs import DDGS
    except ImportError:
        return "Пошук на сервері не встановлений."
    try:
        hits = DDGS().text(query, max_results=limit) or []
    except Exception as e:
        log.warning(f"Пошук «{query}»: {e}")
        return "Пошук зараз не відповідає."
    if not hits:
        return "Нічого не знайшлось."
    return "\n".join(f"- {h.get('title', '')}: {h.get('body', '')[:300]} ({h.get('href', '')})" for h in hits)


# ── Нагадування ──────────────────────────────────────────────────────────────

def parse_when(text: str, base: datetime = None):
    """Коли нагадати: «30» (хвилин), «18:00» (сьогодні, а якщо вже минуло, то завтра),
    «завтра 09:00», «2026-11-09 09:00». Не зрозуміло → None."""
    base, t = base or now(), (text or "").strip().lower()
    if re.fullmatch(r"\d{1,4}", t):
        return base + timedelta(minutes=int(t))
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        day = datetime.strptime(m.group(1), "%Y-%m-%d")
        return base.replace(year=day.year, month=day.month, day=day.day, hour=int(m.group(2)),
                            minute=int(m.group(3)), second=0, microsecond=0)
    m = re.fullmatch(r"(завтра\s+)?(\d{1,2})[:.](\d{2})", t)
    if m:
        at = base.replace(hour=int(m.group(2)) % 24, minute=int(m.group(3)), second=0, microsecond=0)
        if m.group(1):
            return at + timedelta(days=1)
        return at if at > base else at + timedelta(days=1)
    return None


class Reminders:
    """Нагадування на сервері: файл JSON, щоб пережити перезапуск служби."""

    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()

    def _load(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def _save(self, items: list) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, spec: str) -> str:
        """«час|текст» → що сказати Владу."""
        when_text, _, what = (spec or "").partition("|")
        when, what = parse_when(when_text), what.strip()
        if not when or not what:
            return "Не зрозуміла, коли або про що нагадати."
        with self.lock:
            items = self._load()
            items.append({"at": when.isoformat(), "text": what})
            self._save(items)
        return f"Нагадаю {when:%d.%m о %H:%M}: {what}."

    def due(self, at: datetime = None) -> list:
        """Ті, що настали: забрати з файлу й повернути їхні тексти."""
        at = at or now()
        with self.lock:
            items = self._load()
            ready = [i for i in items if datetime.fromisoformat(i["at"]) <= at]
            if ready:
                self._save([i for i in items if i not in ready])
        return [i["text"] for i in ready]
