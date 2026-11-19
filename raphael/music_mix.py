"""
Лін сама підбирає музику в Spotify: підбірка на настрій, конкретний трек, свій плейлист.

Чому окремо: 2026-11-13 на «включи щось прикольне» модель давала spotify_play:chill чи
spotify_play:vibe, а пошук Spotify знаходив трек «CHILL» і «Viber». Тепер на розмите
прохання модель-мозок сама називає конкретні треки («Виконавець - Назва») з огляду на
смак Влада (що слухав і лайкав), Лін знаходить кожен і ставить чергою.

Пошук треку: «Виконавець - Назва» шукається полями track:/artist:, з кількох знайдених
береться найсхожіший за назвою й виконавцем, а не перший. Не знайшла: None, і тоді
вирішує той, хто кликав (у actions це старий пошук через застосунок).
"""
import difflib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from raphael import llm
from raphael import music
from raphael import settings as cfg

log = logging.getLogger("Лін")

MIX_SIZE = 20               # треків у підбірці (≈година)
KNOWN_ARTIST_SHARE = 0.25   # у «новому» щонайбільше стільки від виконавців, яких Влад уже слухає
PLAYLIST_SIZE = 20          # треків у зібраному плейлисті
TASTE_SIZE = 40             # стільки рядків смаку бачить модель
MIN_MATCH = 1.35            # схожість назви + виконавця (0..2), нижче: «не той трек»; з 1.2 проходили
                            # кавери чужих виконавців (M83 - Midnight City → 828 SOUNDWAVES, 2026-11-13)
PLAYLIST_SCOPE = "playlist-modify-private"

# «Щось з мого», «улюблене», «лайкнуте»: тоді можна знайоме; інакше лише нове (2026-11-13 Влад: «не те, що я
# лайкав, його я й так багато слухаю»)
FAMILIAR_RE = re.compile(r"(?i)\bмо[єїя]\w*|улюблен|лайкн|знайом|що я слухаю|що я слухав")
_SPLIT = re.compile(r"\s+[-\u2013\u2014]\s+")


def _tracks(n: int) -> str:
    """«1 трек», «2 треки», «12 треків»."""
    word = ("трек" if n % 10 == 1 and n % 100 != 11 else
            "треки" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "треків")
    return f"{n} {word}"


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def split_pick(line: str):
    """«3. Виконавець - Назва» → (виконавець, назва) або None."""
    line = re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", line or "").strip().strip("\"«»")
    parts = _SPLIT.split(line, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip().strip("\"«»")


def _score(item: dict, artist: str, title: str) -> float:
    names = [a.get("name", "") for a in item.get("artists", [])]
    a = max((_ratio(artist, n) for n in names), default=0) if artist else 0.6
    t = _ratio(title, item.get("name", ""))
    if title and title.lower() in (item.get("name") or "").lower():
        t = max(t, 0.9)                         # «Song (Remastered 2011)» той самий трек
    return a + t


def find(sp, artist: str, title: str):
    """Трек у Spotify → (uri, «Виконавець - Назва») або None, якщо схожого нема."""
    queries = ([f'track:"{title}" artist:"{artist}"', f"{artist} {title}"] if artist else [title])
    best, best_score = None, 0.0
    for q in queries:
        try:
            items = (sp.search(q=q, type="track", limit=5).get("tracks") or {}).get("items") or []
        except Exception as e:
            log.warning(f"Spotify пошук «{q}»: {e}")
            continue
        for it in items:
            s = _score(it, artist, title)
            if s > best_score:
                best, best_score = it, s
        if best_score >= 1.6:
            break
    if not best or best_score < MIN_MATCH:
        return None
    return best["uri"], f"{best['artists'][0]['name']} - {best['name']}"


def find_query(sp, query: str):
    """Що сказав Влад («Виконавець - Назва» чи просто назва) → (uri, підпис) або None."""
    pick = split_pick(query)
    if pick:
        return find(sp, *pick)
    try:
        items = (sp.search(q=query, type="track", limit=5).get("tracks") or {}).get("items") or []
    except Exception as e:
        log.warning(f"Spotify пошук «{query}»: {e}")
        return None
    scored = [(max(_ratio(query, it["name"]), _ratio(query, f"{it['artists'][0]['name']} {it['name']}")), it)
              for it in items]
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.45:
        return None
    it = scored[0][1]
    return it["uri"], f"{it['artists'][0]['name']} - {it['name']}"


def taste(sp):
    """Що Влад слухав і лайкав → (рядки «Виконавець - Назва» і улюблені виконавці, якщо є дозвіл;
    множина uri цих треків, щоб не підсовувати йому вже знайоме)."""
    out, known = [], set()
    try:
        for it in sp.current_user_recently_played(limit=50).get("items", []):
            out.append(f"{it['track']['artists'][0]['name']} - {it['track']['name']}")
            known.add(it["track"].get("uri"))
            known.add("artist:" + it["track"]["artists"][0]["name"].lower())
    except Exception as e:
        log.debug(f"Смак: нещодавні: {e}")
    try:
        for it in sp.current_user_saved_tracks(limit=50).get("items", []):
            out.append(f"{it['track']['artists'][0]['name']} - {it['track']['name']}")
            known.add(it["track"].get("uri"))
            known.add("artist:" + it["track"]["artists"][0]["name"].lower())
    except Exception as e:
        log.debug(f"Смак: лайкнуті: {e}")
    try:
        tok = sp.auth_manager.cache_handler.get_cached_token() or {}
        if "user-top-read" not in (tok.get("scope") or "").split():
            raise PermissionError("нема дозволу user-top-read")   # інакше spotipy пише ERROR 403 щоразу
        for a in sp.current_user_top_artists(limit=15, time_range="medium_term").get("items", []):
            out.append(f"улюблений виконавець: {a['name']}")
            known.add("artist:" + a["name"].lower())
    except Exception as e:
        log.debug(f"Смак: улюблені виконавці (потрібен user-top-read): {e}")
    return list(dict.fromkeys(out))[:TASTE_SIZE], known


def ask_picks(request: str, taste_lines: list, n: int, familiar: bool = False) -> list:
    """Модель-мозок називає n конкретних треків під прохання → [(виконавець, назва)]."""
    known = "\n".join(taste_lines) or "(нема даних)"
    prompt = (f"Влад просить музику: «{request}».\nЩо він останнім часом слухав і лайкав:\n{known}\n\n"
              f"Підбери {n} конкретних треків, які точно є в Spotify і пасують до прохання та його смаку. "
              + ("Можна брати треки з його списку й схожі на них. " if familiar else
                 "Лише НОВЕ для нього: жодного треку з цього списку і переважно НОВІ для нього виконавці (від тих, "
                 "що в списку, щонайбільше кожен четвертий трек); не найвідоміші хіти, а те, що він навряд чи чув, "
                 "але того ж духу. Його список лише напрямок смаку. ")
              + "Різні виконавці, щонайбільше два треки на виконавця. "
              "Якщо прохання про конкретного виконавця чи жанр, тримайся його. "
              f"Відповідь: рівно {n} рядків «Виконавець - Назва», без нумерації, лапок і коментарів.")
    resp = llm.llm_chat(cfg.GROQ_PRIMARY_MODEL, messages=[{"role": "user", "content": prompt}],
                        max_tokens=1500, temperature=0.9)
    text = llm.strip_thinking(resp.choices[0].message.content or "")
    picks = [p for p in (split_pick(l) for l in text.splitlines()) if p]
    return list(dict.fromkeys(picks))[: n + 3]


def _fresh(tracks: list, known: set, limit: int) -> list:
    """Лише нове: без уже слуханих треків, а від знайомих виконавців щонайбільше KNOWN_ARTIST_SHARE."""
    out, familiar_left = [], int(limit * KNOWN_ARTIST_SHARE)
    for uri, label in tracks:
        if uri in known:
            continue
        if "artist:" + label.split(" - ", 1)[0].lower() in known:
            if familiar_left <= 0:
                continue
            familiar_left -= 1
        out.append((uri, label))
    return out[:limit]


def _resolve(sp, picks: list) -> list:
    """Знайти всі вибрані треки паралельно, зберігаючи порядок; не знайдені пропустити."""
    with ThreadPoolExecutor(6) as ex:
        found = list(ex.map(lambda p: find(sp, *p), picks))
    out, seen = [], set()
    for f in found:
        if f and f[0] not in seen:
            seen.add(f[0])
            out.append(f)
    return out


def mix(request: str) -> str:
    """Дія spotify_mix: підібрати й увімкнути підбірку. → що сказати Владу."""
    sp = music._get_spotipy()
    if not sp:
        return music._spotify_unavailable_msg()
    request = (request or "").strip() or "щось на мій смак"
    familiar = bool(FAMILIAR_RE.search(request))
    lines, known = taste(sp)
    picks = ask_picks(request, lines, MIX_SIZE + 8, familiar)       # із запасом: частину відсіємо
    found = _resolve(sp, picks)
    tracks = found[:MIX_SIZE] if familiar else _fresh(found, known, MIX_SIZE)
    log.info(f"Spotify підбірка «{request}»: модель назвала {len(picks)}, знайшла {len(tracks)}: "
             f"{[t[1] for t in tracks]}")
    if not tracks:
        return "Придумала треки, але в Spotify жодного не знайшла. Скажи інакше, що хочеш."
    music.with_device(sp, lambda: sp.start_playback(uris=[t[0] for t in tracks]))
    _started(f"підбірка на прохання «{request}»")
    return f"Поставила {_tracks(len(tracks))}. Почнемо з {tracks[0][1]}."


def _started(about: str):
    """Сказати вайбам, що музику ввімкнула сама Лін (щоб не коментували одразу й не вигадували)."""
    try:
        from raphael import vibes
        vibes.lin_started_music(about)
    except Exception as e:
        log.debug(f"Вайби не знають про музику: {e}")


def play(query: str):
    """Дія spotify_play з конкретним треком: найкращий збіг. → підпис або None (не знайшла)."""
    sp = music._get_spotipy()
    if not sp:
        return None
    hit = find_query(sp, query)
    if hit:
        music.with_device(sp, lambda: sp.start_playback(uris=[hit[0]]))
        _started(f"трек {hit[1]} на прохання Влада")
        log.info(f"Spotify: «{query}» → {hit[1]}")
        return hit[1]
    return None


def make_playlist(spec: str) -> str:
    """Дія spotify_make_playlist:назва|опис: зібрати плейлист у Влада в Spotify і ввімкнути."""
    sp = music._get_spotipy()
    if not sp:
        return music._spotify_unavailable_msg()
    name, _, about = (spec or "").partition("|")
    name = " ".join(name.split())[:80] or "Від Лін"
    about = about.strip() or name
    tok = sp.auth_manager.cache_handler.get_cached_token() or {}
    if PLAYLIST_SCOPE not in (tok.get("scope") or "").split():
        return ("Збирати плейлисти мені Spotify ще не дозволив. Запусти spotify_auth.bat і увійди ще раз, "
                "тоді зможу. А поки можу просто ввімкнути підбірку.")
    familiar = bool(FAMILIAR_RE.search(spec or ""))
    lines, known = taste(sp)
    picks = ask_picks(about, lines, PLAYLIST_SIZE + 8, familiar)
    found = _resolve(sp, picks)
    tracks = found[:PLAYLIST_SIZE] if familiar else _fresh(found, known, PLAYLIST_SIZE)
    if not tracks:
        return "Придумала треки, але в Spotify жодного не знайшла, плейлист не створювала."
    me = sp.current_user()["id"]
    pl = sp.user_playlist_create(me, name, public=False, description=f"Зібрала Лін: {about}"[:300])
    sp.playlist_add_items(pl["id"], [t[0] for t in tracks])
    log.info(f"Spotify плейлист «{name}»: {[t[1] for t in tracks]}")
    music.with_device(sp, lambda: sp.start_playback(context_uri=pl["uri"]))
    _started(f"плейлист «{name}»: {about}")
    return f"Зібрала плейлист «{name}», {_tracks(len(tracks))}, і вмикаю."
