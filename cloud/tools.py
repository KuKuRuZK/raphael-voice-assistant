"""Дії серверної Лін: погода, пошук і читання сторінок, нагадування в Telegram."""
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timedelta
from html.parser import HTMLParser

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


# ── Новини ───────────────────────────────────────────────────────────────────

def news(topic: str = "") -> str:
    """Свіжі заголовки зі стрічок з датами й джерелами (raphael/news.py), з вказівкою переказу."""
    from raphael import news as feeds
    return feeds.digest(feeds.headlines(topic)) + "\n\n" + feeds.RETELL


# ── Читання сторінки ─────────────────────────────────────────────────────────
# Адресу дає модель, а модель читала чужі сторінки: будь-яка з них може попросити
# «відкрий http://127.0.0.1:8080/...». На цьому сервері поруч магазин з адмінкою й
# базою, тож лише публічні адреси, і кожне перенаправлення перевіряється знову.

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return (p.title.strip() + "\n" if p.title.strip() else "") + text


def public_host(host: str) -> bool:
    """Чи всі адреси хоста публічні (не localhost, не приватна мережа, не метадані хмари)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def read_page(url: str, limit: int = 10000) -> str:
    """Текст сторінки (без розмітки, скриптів і меню з <head>), обрізаний до limit."""
    url = (url or "").strip()
    for _ in range(5):                                      # перенаправлення вручну, кожне з перевіркою
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return "Читаю лише сторінки http(s)."
        if not public_host(u.hostname):
            return "Це внутрішня або недоступна адреса, її не читаю."
        try:
            r = httpx.get(url, timeout=15, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 (Lin; personal assistant)"})
        except Exception as e:
            return f"Сторінка не відкрилась: {type(e).__name__}."
        if r.is_redirect and r.headers.get("location"):
            url = urllib.parse.urljoin(url, r.headers["location"])
            continue
        break
    else:
        return "Забагато перенаправлень."
    if r.status_code >= 400:
        return f"Сторінка відповіла {r.status_code}."
    kind = r.headers.get("content-type", "")
    if "html" not in kind and "text" not in kind and "json" not in kind:
        return f"Це не текстова сторінка ({kind or 'невідомо що'})."
    text = html_text(r.text[:2_000_000]) if "html" in kind else r.text
    return text[:limit] or "Сторінка порожня."


# ── Факти вакансії ───────────────────────────────────────────────────────────
# 2026-11-08 вартовий за самим списком djinni написав Broscorp «гібрид у Вільнюсі», а на
# сторінці вакансії Full Remote і кандидати лише з України; Insiders (офіс у Львові) включив
# усупереч критеріям. Тепер агент перед тим, як включити вакансію, бере з її сторінки лише
# рядки про формат, місто, країни кандидатів, досвід і зарплату: коротко й без домислів.
_JOB_FACT = re.compile(
    r"(?i)remote|віддален|удален|nuotolin|office|офіс|офис|biur|hybrid|гібрид|гибрид|hibrid|"
    r"vilnius|вільнюс|вильнюс|lithuania|литв|lietuv|lviv|львів|kyiv|київ|warsaw|варшав|"
    r"countr|країн|стран|europe|європ|европ|worldwide|anywhere|eu\b|ukrain|україн|poland|польщ|"
    r"experience|досвід|опыт|patirt|years?\b|рок|год|junior|middle|senior|lead|trainee|"
    r"salary|зарплат|зп\b|\$|€|eur|usd|atlyginim|english|англійськ|английск")


def job_facts(url: str) -> str:
    """Назва вакансії й лише рядки з фактами (формат, місто, країни, досвід, зарплата, мова)."""
    text = read_page(url, limit=20000)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        return text
    facts = []
    for i, line in enumerate(lines[1:], 1):
        if len(line) < 200 and _JOB_FACT.search(line):
            facts.append(line)
            # Після заголовка «Countries where we consider candidates» чи «Office:» самі країни
            # й місто стоять наступними рядками і власних ознак можуть не мати
            if re.search(r"(?i)countr|країн|стран|office:|офіс:", line):
                facts += [l for l in lines[i + 1:i + 4] if len(l) < 80]
    return lines[0] + "\n" + "\n".join(dict.fromkeys(facts[:30]))


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
