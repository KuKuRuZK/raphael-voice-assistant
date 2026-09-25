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
                cfg.GROQ_PRIMARY_MODEL,
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


def _get_weather(city: str = None) -> str:
    """Отримує погоду через wttr.in (без API ключа)."""
    city = city or cfg.WEATHER_CITY
    try:
        import urllib.request, urllib.parse
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=uk"
        req = urllib.request.Request(url, headers={"User-Agent": "Lin/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())

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

        rain_note = f" Завтра ймовірність дощу {rain_chance}%." if rain_chance >= 40 else ""
        result = (
            f"{desc}, {temp}°, відчувається як {feels}°. "
            f"Вологість {humidity}%, вітер {wind} км/г. "
            f"Завтра від {t_min} до {t_max}°.{rain_note}"
        )
        log.info(f"Weather: {result}")
        return result

    except Exception as e:
        log.error(f"Weather error: {e}")
        return "Не вдалося отримати погоду. Перевір інтернет."


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
