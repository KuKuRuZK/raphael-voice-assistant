"""
Інтернет: погода, курси валют, тихий пошук з відповіддю голосом.
"""
import json
import logging
import threading

from raphael import llm
from raphael import settings as cfg
from raphael import tts

log = logging.getLogger("Лін")


def _news(topic: str = "") -> None:
    """Дія news: свіжі новини зі стрічок (raphael/news.py), переказ голосом чи в Telegram."""
    tts.speak("Гляну стрічки...")

    def _run():
        from raphael import news
        try:
            items = news.headlines(topic)
            if not items:
                tts.speak("У стрічках за добу нічого не знайшла" + (f" про «{topic}»." if topic else "."))
                return
            log.info(f"Новини{' про ' + topic if topic else ''}: {len(items)} заголовків")
            # Переказ завжди хмарною моделлю: локальна на довгому списку думає довго
            answer = tts.speak_stream(llm.llm_stream(
                cfg.GROQ_PRIMARY_MODEL,
                messages=[{"role": "user", "content": f"{news.RETELL}\n\n{news.digest(items)}"}],
                max_tokens=500, temperature=0.3,
            ))
            log.info(f"Новини: {answer[:120]}")
        except Exception as e:
            log.error(f"Новини: {e}", exc_info=True)
            tts.speak("Не вдалося дістати новини.")

    threading.Thread(target=_run, daemon=True).start()


def _silent_web_search(query: str):
    """Тихий пошук без відкриття браузера. Результат зачитується вголос."""
    tts.speak("Шукаю...")

    def _run():
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            results = DDGS().text(query, max_results=4)

            if not results:
                tts.speak("Нічого не знайшла по цьому запиту.")
                return

            # Збираємо сніпети
            snippets = []
            for r in results:
                body = r.get("body", "").strip()
                title = r.get("title", "").strip()
                if body:
                    snippets.append(f"{title}: {body[:300]}")

            combined = "\n".join(snippets[:3])
            log.info(f"Web search '{query}': {len(snippets)} результатів")

            # Модель підсумовує коротко, озвучка йде потоком
            answer = tts.speak_stream(llm.llm_stream(
                llm.main_model(),
                messages=[{
                    "role": "user",
                    "content": (
                        f"На основі цих результатів пошуку дай КОРОТКУ відповідь "
                        f"(2-3 речення) українською на питання: «{query}»\n\n{combined}"
                    )
                }],
                max_tokens=180,
                temperature=0.4,
            ))
            log.info(f"Web answer: {answer[:120]}")

        except Exception as e:
            log.error(f"Silent search error: {e}", exc_info=True)
            tts.speak("Не вдалося знайти інформацію.")

    threading.Thread(target=_run, daemon=True).start()


def _get_json(url: str) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Lin/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _weather_wttr(city: str) -> str:
    """wttr.in: без ключа, з українським описом."""
    import urllib.parse
    data = _get_json(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=uk")

    cur = data["current_condition"][0]
    temp     = cur["temp_C"]
    feels    = cur["FeelsLikeC"]
    humidity = cur["humidity"]
    wind     = cur["windspeedKmph"]

    # Опис — спробуємо Ukrainian, інакше English
    lang_list = cur.get("lang_uk") or cur.get("weatherDesc", [])
    desc = lang_list[0]["value"] if lang_list else "невідомо"

    # Завтра
    tomorrow  = data["weather"][1]
    t_max     = tomorrow["maxtempC"]
    t_min     = tomorrow["mintempC"]
    hourly    = tomorrow.get("hourly", [])
    rain_chance = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)
    return _weather_text(desc, temp, feels, humidity, wind, t_min, t_max, rain_chance)


# Коди погоди WMO (Open-Meteo) → опис
_WMO = {0: "Ясно", 1: "Переважно ясно", 2: "Мінлива хмарність", 3: "Хмарно", 45: "Туман", 48: "Туман з інеєм",
        51: "Мряка", 53: "Мряка", 55: "Сильна мряка", 56: "Крижана мряка", 57: "Крижана мряка",
        61: "Невеликий дощ", 63: "Дощ", 65: "Сильний дощ", 66: "Крижаний дощ", 67: "Крижаний дощ",
        71: "Невеликий сніг", 73: "Сніг", 75: "Сильний сніг", 77: "Снігова крупа",
        80: "Злива", 81: "Злива", 82: "Сильна злива", 85: "Снігопад", 86: "Сильний снігопад",
        95: "Гроза", 96: "Гроза з градом", 99: "Гроза з градом"}


def _weather_open_meteo(city: str) -> str:
    """Open-Meteo: теж без ключа. Резерв, коли wttr.in лежить: 2026-11-03 його
    сервер відкидав зʼєднання (WinError 10061), а інтернет працював."""
    import urllib.parse
    found = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=uk&name="
                      + urllib.parse.quote(city)).get("results") or []
    if not found:
        raise ValueError(f"місто «{city}» не знайдено")
    lat, lon = found[0]["latitude"], found[0]["longitude"]
    data = _get_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=2"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max")
    cur, day = data["current"], data["daily"]
    return _weather_text(_WMO.get(cur.get("weather_code"), "Погода"),
                         round(cur["temperature_2m"]), round(cur["apparent_temperature"]),
                         round(cur["relative_humidity_2m"]), round(cur["wind_speed_10m"]),
                         round(day["temperature_2m_min"][1]), round(day["temperature_2m_max"][1]),
                         int(day.get("precipitation_probability_max", [0, 0])[1] or 0))


def _weather_text(desc, temp, feels, humidity, wind, t_min, t_max, rain_chance) -> str:
    rain_note = f" Завтра ймовірність дощу {rain_chance}%." if rain_chance >= 40 else ""
    return (
        f"{desc}, {temp}°, відчувається як {feels}°. "
        f"Вологість {humidity}%, вітер {wind} км/г. "
        f"Завтра від {t_min} до {t_max}°.{rain_note}"
    )


def _get_weather(city: str = None) -> str:
    """Погода: wttr.in, а якщо він не відповідає, Open-Meteo."""
    city = city or cfg.WEATHER_CITY
    errors = []
    for source in (_weather_wttr, _weather_open_meteo):
        try:
            result = source(city)
            log.info(f"Weather ({source.__name__}): {result}")
            return result
        except Exception as e:
            errors.append(f"{source.__name__}: {e}")
    log.error(f"Weather error: {'; '.join(errors)}")
    return "Не вдалося отримати погоду: обидва сервіси погоди не відповіли."


# ============================================================
#  КОНВЕРТЕР ВАЛЮТ
# ============================================================

def _currency_convert(amount: float, from_cur: str, to_cur: str) -> str:
    """Конвертує валюти через open.er-api.com (без ключа, 170+ валют включно з UAH)."""
    try:
        import urllib.request
        from_c = from_cur.upper().strip()
        to_c   = to_cur.upper().strip()
        url = f"https://open.er-api.com/v6/latest/{from_c}"
        req = urllib.request.Request(url, headers={"User-Agent": "Lin/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        if data.get("result") != "success":
            return f"Не вдалося отримати курс {from_c}."
        rate = data["rates"].get(to_c)
        if rate is None:
            return f"Валюта {to_c} не знайдена."
        result_val = round(amount * rate, 2)
        names = {
            "USD": "доларів", "EUR": "євро", "UAH": "гривень",
            "PLN": "злотих",  "GBP": "фунтів", "JPY": "єн",
            "CHF": "франків", "CZK": "крон",    "SEK": "крон",
            "NOK": "крон",    "CAD": "канадських доларів",
            "AUD": "австралійських доларів",     "BYN": "білоруських рублів",
            "RUB": "рублів",  "TRY": "лір",      "CNY": "юанів",
        }
        from_n = names.get(from_c, from_c)
        to_n   = names.get(to_c,   to_c)
        return f"{amount:g} {from_n} = {result_val:g} {to_n}."
    except Exception as e:
        log.error(f"Currency convert: {e}")
        return "Не вдалося конвертувати. Перевір інтернет."
