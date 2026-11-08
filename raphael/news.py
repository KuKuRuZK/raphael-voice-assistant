"""
Новини зі стрічок RSS великих редакцій: заголовки з датою, джерелом і посиланням.

Звичайний пошук дає загальні сторінки без дат, і модель на «дай новини» дописувала
подробиці від себе (2026-11-08: «у Європі стартувала масштабна кібератака на
енергосистеми»), а пошук новин ddgs на «Україна новини» віддав одну новину місячної
давнини. RSS свіжий і перевірний. Лише стандартна бібліотека й httpx: модуль працює
і вдома, і на сервері (cloud/tools.py).
"""
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger("Лін")

try:                        # стрічки з чужих сайтів: без зовнішніх сутностей і «мільярда сміхів»
    from defusedxml.ElementTree import fromstring as _parse_xml
except ImportError:         # свіжий expat у Python 3.12 і так їх не підтягує, але краще з захистом
    from xml.etree.ElementTree import fromstring as _parse_xml

# Побутове з УНІАН і подібних («як зберігати цвітну капусту», «рідкісний богомол») забивало
# список замість новин (2026-11-08)
JUNK = re.compile(r"/(curiosities|lite|lifestyle|horoscope|recipes|ecology|health|style|tourism|sport)/", re.I)

# (назва, адреса, про що): ua Україна, world світ
FEEDS = [
    ("BBC Україна", "https://feeds.bbci.co.uk/ukrainian/rss.xml", "world"),
    ("Українська правда", "https://www.pravda.com.ua/rss/view_news/", "ua"),
    ("УНІАН", "https://rss.unian.net/site/news_ukr.rss", "ua"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
]


def _when(item) -> datetime:
    raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _feed(name: str, url: str, kind: str, since: datetime) -> list:
    import httpx
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Lin)"})
        root = _parse_xml(r.content)
    except Exception as e:
        log.warning(f"Новини: {name} не відповіли: {type(e).__name__}")
        return []
    out = []
    for item in root.iter("item"):
        when = _when(item)
        if when is None or when < since or JUNK.search(item.findtext("link") or ""):
            continue
        out.append({"source": name, "kind": kind, "when": when, "title": _clean(item.findtext("title")),
                    "about": _clean(item.findtext("description"))[:240], "link": (item.findtext("link") or "").strip()})
    return out


def _stem(word: str) -> str:
    """Основа слова без закінчення: «Краків» шукає і «Кракові», «Україна» і «України»."""
    word = word.lower()
    return word[:max(4, len(word) - 2)]


def headlines(topic: str = "", hours: int = 30, per_feed: int = 8) -> list:
    """Свіжі заголовки, новіші першими. topic: лише ті, де є всі значущі слова теми."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with ThreadPoolExecutor(len(FEEDS)) as pool:
        batches = list(pool.map(lambda f: _feed(*f, since), FEEDS))
    words = [_stem(w) for w in re.findall(r"[\wʼ'’-]{4,}", topic or "")]
    picked = []
    for batch in batches:
        if words:
            batch = [n for n in batch if all(w in (n["title"] + " " + n["about"]).lower() for w in words)]
        picked += sorted(batch, key=lambda n: n["when"], reverse=True)[:per_feed]
    return sorted(picked, key=lambda n: n["when"], reverse=True)


def digest(items: list, limit: int = 24) -> str:
    """Список для моделі: час, джерело, заголовок, уривок, посилання."""
    if not items:
        return "Свіжих новин у стрічках не знайшлось."
    return "\n".join(f"{n['when'].astimezone():%H:%M %d.%m} | {n['source']} | {n['title']}"
                     f"{': ' + n['about'] if n['about'] and n['about'] != n['title'] else ''} | {n['link']}"
                     for n in items[:limit])


RETELL = ("Перекажи Владу найважливіше з цих новин коротко, українською: спершу Україна, потім світ, "
          "4-7 пунктів. Лише те, що є в списку, нічого не додавай від себе і не домислюй подробиць; "
          "біля кожного пункту джерело. Розважальні дрібниці пропускай. Звичайний текст без ** і # "
          "(у Telegram розмітку видно як є), пункти через дефіс.")
