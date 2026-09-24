"""
Spotify: відтворення, пауза, гучність, плейлисти, пристрої.
Токен перевіряється тут, без інтерактивного входу (під pythonw його немає).
"""
import difflib
import logging
import threading

import pyautogui

from raphael import settings as cfg
from raphael import spotify_common
from raphael import tts

log = logging.getLogger("Лін")


_SPOTIPY_CLIENT = None   # кешований клієнт — не створюємо заново кожного разу
_spotify_lock = threading.Lock()   # серіалізує доступ до токена (без гонок)

_spotify_problem = ""   # чому Spotify API зараз недоступний; це й кажемо голосом


def _spotify_unavailable_msg() -> str:
    return _spotify_problem or "Spotify API не налаштований."


def _get_spotipy():
    """
    Повертає авторизований Spotipy клієнт або None.

    Ніколи не доходить до інтерактивного входу: без придатного токена spotipy
    сам викликає input(), а під pythonw консолі немає («lost sys.stdin»).
    Тому токен перевіряємо тут: чи є він, чи має всі дозволи (spotify_common),
    і оновлюємо проактивно під замком. Причину відмови кладемо в
    _spotify_problem, щоб сказати голосом, що саме зробити.
    """
    global _SPOTIPY_CLIENT, _spotify_problem
    if not cfg.SPOTIFY_CLIENT_ID or not cfg.SPOTIFY_CLIENT_SECRET:
        _spotify_problem = "Spotify не налаштований: додай ключі в secrets.json."
        return None
    with _spotify_lock:
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
            from spotipy.cache_handler import CacheFileHandler

            if _SPOTIPY_CLIENT is None:
                auth = SpotifyOAuth(
                    client_id=cfg.SPOTIFY_CLIENT_ID,
                    client_secret=cfg.SPOTIFY_CLIENT_SECRET,
                    redirect_uri=spotify_common.REDIRECT_URI,
                    scope=spotify_common.SCOPES,
                    cache_handler=CacheFileHandler(cache_path=spotify_common.TOKEN_PATH),
                    open_browser=False,   # під pythonw браузер/stdin недоступні
                )
                _SPOTIPY_CLIENT = spotipy.Spotify(auth_manager=auth)
                log.info("Spotipy клієнт ініціалізовано")

            am = _SPOTIPY_CLIENT.auth_manager
            tok = am.cache_handler.get_cached_token()
            if not tok:
                _spotify_problem = "Spotify ще не авторизований. Запусти spotify_auth.bat."
                log.error("Spotify: токена немає, потрібен spotify_auth.bat")
                return None
            if not spotify_common.token_has_scopes(tok):
                _spotify_problem = "Токену Spotify бракує дозволів. Запусти spotify_auth.bat ще раз."
                log.error(f"Spotify: у токені не всі дозволи ({tok.get('scope')})")
                return None
            if am.is_token_expired(tok):
                try:
                    am.refresh_access_token(tok["refresh_token"])
                    log.info("Spotify токен оновлено")
                except Exception as e:
                    _spotify_problem = "Не вдалося оновити токен Spotify. Запусти spotify_auth.bat."
                    log.error(f"Spotify refresh: {e}")
                    return None
            _spotify_problem = ""
            return _SPOTIPY_CLIENT
        except Exception as e:
            _spotify_problem = "Spotify зараз не відповідає."
            log.error(f"Spotipy init/refresh: {e}")
            return None


def _spotify_transfer_device(hint: str):
    """Перемикає відтворення Spotify на пристрій за підказкою (телефон/пк/назва)."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            tts.speak("Spotify не знайшов активних пристроїв. Відкрий Spotify на потрібному пристрої.")
            return

        h = hint.lower()
        # Мапи підказок до типів пристроїв
        phone_hints   = {"телефон", "phone", "mobile", "мобільний", "смартфон"}
        pc_hints      = {"пк", "pc", "комп", "комп'ютер", "computer", "ноут", "laptop", "windows"}

        target = None
        for d in devices:
            name = d["name"].lower()
            dtype = d.get("type", "").lower()
            if h in phone_hints and dtype in ("smartphone",):
                target = d; break
            if h in pc_hints and dtype in ("computer",):
                target = d; break
            if h in name:
                target = d; break

        if not target:
            # Fuzzy match по імені пристрою
            names = [d["name"] for d in devices]
            close = difflib.get_close_matches(hint, [n.lower() for n in names], n=1, cutoff=0.4)
            if close:
                target = next(d for d in devices if d["name"].lower() == close[0])

        if not target:
            names_str = ", ".join([f"{d['name']} ({d['type']})" for d in devices])
            tts.speak(f"Знайдені пристрої: {names_str}. Скажи точніше.")
            return

        sp.transfer_playback(target["id"], force_play=True)
        tts.speak(f"Перемикаю відтворення на {target['name']}.")
        log.info(f"Spotify device: {target['name']}")
    except Exception as e:
        log.error(f"Spotify device помилка: {e}")
        tts.speak("Не вдалося перемкнути пристрій.")


def _spotify_set_playing(want_playing: bool):
    """
    Пауза або продовження САМЕ як сказано. Раніше і «пауза», і «грай» були
    перемикачем: «постав на паузу», коли вже на паузі, вмикало музику.
    Без API лишається тільки медіа-клавіша, а вона вміє лише перемикати.
    """
    sp = _get_spotipy()
    if sp:
        try:
            state = sp.current_playback()
            playing = bool(state and state.get("is_playing"))
            if playing == want_playing:
                log.info(f"Spotify: вже {'грає' if playing else 'на паузі'}, нічого не міняю")
                return
            if want_playing:
                sp.start_playback()
            else:
                sp.pause_playback()
            log.info(f"Spotify API: {'resumed' if want_playing else 'paused'}")
            return
        except Exception as e:
            log.warning(f"Spotify pause/resume API fail: {e}")
    pyautogui.hotkey("playpause")
    log.info("Spotify: media key playpause")


def _spotify_now_playing():
    """Вголос каже що зараз грає."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        state = sp.current_playback()
        if not state or not state.get("item"):
            tts.speak("Зараз нічого не грає.")
            return
        item   = state["item"]
        track  = item["name"]
        artist = ", ".join(a["name"] for a in item["artists"])
        album  = item["album"]["name"]
        is_playing = state.get("is_playing", False)
        status = "Грає" if is_playing else "На паузі"
        tts.speak(f"{status}: {track} — {artist}, альбом {album}.")
        log.info(f"Spotify now: {track} / {artist}")
    except Exception as e:
        log.error(f"Spotify now playing: {e}")
        tts.speak("Не вдалося отримати інфо про трек.")


def _spotify_recent(limit: int = 5):
    """Вголос каже нещодавно прослухані треки."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_recently_played(limit=limit)
        items = results.get("items", [])
        if not items:
            tts.speak("Нещодавно прослуханих треків не знайдено.")
            return
        seen = []
        lines = []
        for it in items:
            track  = it["track"]["name"]
            artist = it["track"]["artists"][0]["name"]
            key    = f"{track}|{artist}"
            if key not in seen:
                seen.append(key)
                lines.append(f"{track} від {artist}")
            if len(lines) >= 3:
                break
        tts.speak("Нещодавно слухав: " + "; ".join(lines) + ".")
        log.info(f"Spotify recent: {lines}")
    except Exception as e:
        log.error(f"Spotify recent: {e}")
        tts.speak("Не вдалося отримати список.")


def _spotify_liked(limit: int = 5):
    """Вголос каже кілька лайкнутих треків."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_saved_tracks(limit=limit)
        items = results.get("items", [])
        if not items:
            tts.speak("Лайкнутих треків не знайдено.")
            return
        lines = []
        for it in items:
            track  = it["track"]["name"]
            artist = it["track"]["artists"][0]["name"]
            lines.append(f"{track} від {artist}")
        tts.speak("Твої лайкнуті: " + "; ".join(lines) + ".")
        log.info(f"Spotify liked: {lines}")
    except Exception as e:
        log.error(f"Spotify liked: {e}")
        tts.speak("Не вдалося отримати лайкнуті треки.")


def _spotify_pick_device(sp):
    """
    Знаходить пристрій Spotify для керування гучністю.
    Повертає (device_id, volume_percent) або (None, None).
    Спочатку активний пристрій, потім будь-який доступний (краще комп'ютер).
    """
    # 1. Активний пристрій з current_playback
    try:
        state = sp.current_playback()
        if state and state.get("device") and state["device"].get("id"):
            d = state["device"]
            return d["id"], d.get("volume_percent")
    except Exception as e:
        log.debug(f"_spotify_pick_device current_playback: {e}")

    # 2. Будь-який пристрій зі списку (desktop app буде тут навіть якщо не «активний»)
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            return None, None
        active = next((d for d in devices if d.get("is_active")), None)
        computer = next((d for d in devices if d.get("type") == "Computer"), None)
        chosen = active or computer or devices[0]
        return chosen["id"], chosen.get("volume_percent")
    except Exception as e:
        log.debug(f"_spotify_pick_device devices: {e}")
        return None, None


def _spotify_volume(direction: str, value: int = 10) -> None:
    """
    Змінює гучність САМЕ Spotify через API (ніколи не чіпає системну гучність).
    direction: "up" | "down" | "set"
    """
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return

    device_id, current_vol = _spotify_pick_device(sp)
    if device_id is None:
        tts.speak("Spotify не запущений на жодному пристрої. Відкрий застосунок і увімкни щось.")
        return
    if current_vol is None:
        current_vol = 50

    if direction == "up":
        new_vol = min(current_vol + value, 100)
    elif direction == "down":
        new_vol = max(current_vol - value, 0)
    else:  # "set"
        new_vol = max(0, min(value, 100))

    try:
        sp.volume(new_vol, device_id=device_id)
        log.info(f"Spotify volume: {current_vol}% → {new_vol}% (device {device_id[:8]})")
        tts.speak(f"Гучність Spotify {new_vol} відсотків.")
    except Exception as e:
        es = str(e)
        log.warning(f"Spotify volume API fail: {es}")
        if "403" in es or "premium" in es.lower():
            tts.speak("Керування гучністю Spotify працює тільки з Premium підпискою.")
        elif "VOLUME" in es.upper() or "control device volume" in es.lower():
            tts.speak("Цей пристрій не дозволяє керувати гучністю. Спробуй на телефоні.")
        else:
            tts.speak("Не вдалося змінити гучність Spotify.")


def _maybe_spotify_volume(direction: str) -> bool:
    """
    Для НЕЯВНОЇ команди гучності ('гучніше' без слова spotify):
    керуємо Spotify лише якщо він зараз АКТИВНЕ джерело звуку
    (грає або має активний пристрій). Інакше False → системна гучність.
    """
    sp = _get_spotipy()
    if not sp:
        return False
    try:
        # Грає прямо зараз?
        state = sp.current_playback()
        if state and state.get("is_playing"):
            _spotify_volume(direction)
            return True
        # Або є активний пристрій (на паузі, але обраний у Connect)?
        devices = sp.devices().get("devices", [])
        if any(d.get("is_active") for d in devices):
            _spotify_volume(direction)
            return True
    except Exception as e:
        log.debug(f"_maybe_spotify_volume: {e}")
    return False


def _spotify_shuffle(enable: bool) -> None:
    """Вмикає або вимикає shuffle через API."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        sp.shuffle(enable)
        state_str = "увімкнено" if enable else "вимкнено"
        log.info(f"Spotify shuffle: {state_str}")
        tts.speak(f"Перемішування {state_str}.")
    except Exception as e:
        log.error(f"Spotify shuffle: {e}")
        tts.speak("Не вдалося змінити режим перемішування.")


def _spotify_repeat(mode: str) -> None:
    """
    Встановлює режим повтору через API.
    mode: "track" | "context" | "off"
    """
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    mode_map = {
        "track": "track", "трек": "track", "пісня": "track", "один": "track",
        "context": "context", "плейлист": "context", "альбом": "context", "все": "context",
        "off": "off", "вимкнути": "off", "ні": "off", "стоп": "off",
    }
    api_mode = mode_map.get(mode.lower(), "off")
    try:
        sp.repeat(api_mode)
        labels = {"track": "повтор треку", "context": "повтор плейлисту", "off": "повтор вимкнено"}
        log.info(f"Spotify repeat: {api_mode}")
        tts.speak(f"{labels[api_mode]}.")
    except Exception as e:
        log.error(f"Spotify repeat: {e}")
        tts.speak("Не вдалося змінити режим повтору.")


def _spotify_playlists(limit: int = 10) -> None:
    """Вголос перераховує плейлисти користувача."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.current_user_playlists(limit=limit)
        items = results.get("items", [])
        if not items:
            tts.speak("Плейлистів не знайдено.")
            return
        names = [it["name"] for it in items if it]
        tts.speak("Твої плейлисти: " + ", ".join(names) + ".")
        log.info(f"Spotify playlists: {names}")
    except Exception as e:
        log.error(f"Spotify playlists: {e}")
        tts.speak("Не вдалося отримати плейлисти.")


def _spotify_play_playlist(name: str) -> None:
    """Шукає плейлист за назвою і відтворює його."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        # Спочатку шукаємо серед власних плейлистів
        results = sp.current_user_playlists(limit=50)
        items = [it for it in results.get("items", []) if it]
        names = [it["name"] for it in items]

        # Fuzzy пошук
        close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
        if close:
            playlist = next(it for it in items if it["name"] == close[0])
        else:
            # Якщо не знайшли серед своїх — глобальний пошук
            sr_res = sp.search(q=name, type="playlist", limit=1)
            pl_items = sr_res.get("playlists", {}).get("items", [])
            if not pl_items:
                tts.speak(f"Плейлист «{name}» не знайдено.")
                return
            playlist = pl_items[0]

        sp.start_playback(context_uri=playlist["uri"])
        log.info(f"Spotify play playlist: {playlist['name']}")
        tts.speak(f"Вмикаю плейлист «{playlist['name']}».")
    except Exception as e:
        log.error(f"Spotify play playlist: {e}")
        if "No active device" in str(e):
            tts.speak("Відкрий Spotify на телефоні або ПК, щоб я могла відтворити.")
        else:
            tts.speak("Не вдалося відтворити плейлист.")


def _spotify_queue(limit: int = 5) -> None:
    """Показує наступні треки в черзі."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        queue_data = sp.queue()
        queue = queue_data.get("queue", [])
        if not queue:
            tts.speak("Черга порожня.")
            return
        lines = []
        for t in queue[:limit]:
            track  = t["name"]
            artist = t["artists"][0]["name"]
            lines.append(f"{track} від {artist}")
        tts.speak("Далі в черзі: " + "; ".join(lines) + ".")
        log.info(f"Spotify queue: {lines}")
    except Exception as e:
        log.error(f"Spotify queue: {e}")
        tts.speak("Не вдалося отримати чергу.")


def _spotify_playlist_tracks(name: str) -> None:
    """
    Зачитує перші 10 треків з плейлисту.
    Якщо name порожній — з поточного контексту відтворення.
    """
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        if not name:
            # Беремо поточний контекст
            state = sp.current_playback()
            if not state or not state.get("context"):
                tts.speak("Зараз не грає жоден плейлист.")
                return
            ctx = state["context"]
            if ctx["type"] != "playlist":
                tts.speak("Зараз грає не плейлист.")
                return
            pl_id = ctx["uri"].split(":")[-1]
            pl_info = sp.playlist(pl_id, fields="name,tracks.items(track(name,artists))")
            pl_name = pl_info["name"]
            tracks_raw = pl_info["tracks"]["items"]
        else:
            # Шукаємо плейлист за назвою
            results = sp.current_user_playlists(limit=50)
            items = [it for it in results.get("items", []) if it]
            names = [it["name"] for it in items]
            close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
            if not close:
                tts.speak(f"Плейлист «{name}» не знайдено.")
                return
            playlist = next(it for it in items if it["name"] == close[0])
            pl_name = playlist["name"]
            pl_info = sp.playlist(playlist["id"], fields="tracks.items(track(name,artists))")
            tracks_raw = pl_info["tracks"]["items"]

        lines = []
        for it in tracks_raw[:10]:
            t = it.get("track")
            if t:
                lines.append(f"{t['name']} — {t['artists'][0]['name']}")

        if not lines:
            tts.speak(f"В плейлисті «{pl_name}» немає треків.")
            return

        tts.speak(f"Плейлист «{pl_name}»: " + "; ".join(lines) + ".")
        log.info(f"Playlist tracks '{pl_name}': {len(lines)} треків")
    except Exception as e:
        log.error(f"Spotify playlist tracks: {e}")
        tts.speak("Не вдалося отримати треки плейлисту.")


def _spotify_add_to_queue(query: str) -> None:
    """Додає трек в чергу Spotify за назвою."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        results = sp.search(q=query, type="track", limit=1)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            tts.speak(f"Трек «{query}» не знайдено.")
            return
        track = tracks[0]
        sp.add_to_queue(track["uri"])
        name = track["name"]
        artist = track["artists"][0]["name"]
        log.info(f"Spotify queue add: {name} / {artist}")
        tts.speak(f"Додала «{name}» від {artist} в чергу.")
    except Exception as e:
        log.error(f"Spotify add to queue: {e}")
        if "No active device" in str(e):
            tts.speak("Відкрий Spotify на пристрої, щоб додати в чергу.")
        else:
            tts.speak("Не вдалося додати в чергу.")


def _spotify_list_devices():
    """Голосом повідомляє список активних пристроїв Spotify."""
    sp = _get_spotipy()
    if not sp:
        tts.speak(_spotify_unavailable_msg())
        return
    try:
        devices = sp.devices().get("devices", [])
        if not devices:
            tts.speak("Активних пристроїв Spotify не знайдено.")
            return
        names = [f"{d['name']} ({d['type']})" for d in devices]
        tts.speak("Доступні пристрої Spotify: " + ", ".join(names))
    except Exception as e:
        log.error(f"Spotify list devices: {e}")
