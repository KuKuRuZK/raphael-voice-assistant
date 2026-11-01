"""
Вайби: Рафаель сама помічає гру й музику і інколи вставляє своє.

Гра = процес з теки Steam/Epic/GOG або exe зі списку VIBE_GAME_EXES.
Музика = зміна треку в Spotify. Що зараз грає, дописується в системний
промпт (context), тож і звичайні відповіді в темі. Реплік від себе не більше
VIBE_MAX_PER_HOUR на годину; мовчить, коли й так говорить чи слухає, у
фокус-режимі і після vibe_off. Налаштування VIBE_* у settings і config.json.
"""
import logging
import random
import time

import psutil

from raphael import llm
from raphael import music
from raphael import runtime
from raphael import settings as cfg
from raphael import timers
from raphael import tts

log = logging.getLogger("Лін")

_GAME_ROOTS = ("\\steamapps\\common", "\\epic games", "\\gog galaxy\\games")
# Службові exe в теках ігор: деінсталятори й репортери падінь
_NOT_GAME_EXE = ("unins", "crashreport", "unitycrash")

_vibe = {"game": None, "game_since": 0.0, "game_last_quip": 0.0,
          "track": None, "artist": None, "music_last_quip": 0.0, "said": []}


def toggle(on: bool) -> str:
    cfg.VIBE_ENABLED = on
    log.info(f"Вайби: {'увімкнено' if on else 'вимкнено'}")
    return "Окей, я знову з тобою." if on else "Ладно, мовчу. Скучиш, то клич."


def game_from_process(name: str, exe: str):
    """Назва гри для одного процесу або None. Окремо від psutil, щоб тестувати."""
    name = (name or "").lower()
    exes = {k.lower(): v for k, v in cfg.VIBE_GAME_EXES.items()}
    if name in exes:
        return exes[name]
    low = (exe or "").lower()
    for root in _GAME_ROOTS:
        i = low.find(root + "\\")
        if i < 0:
            continue
        folder = exe[i + len(root) + 1:].split("\\", 1)[0]
        ignore = {x.lower() for x in cfg.VIBE_GAME_IGNORE}
        if folder and folder.lower() not in ignore and not name.startswith(_NOT_GAME_EXE):
            return folder
    return None


def detect_game():
    """→ назва гри, що зараз запущена, або None."""
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            game = game_from_process(proc.info.get("name"), proc.info.get("exe"))
        except Exception:
            continue
        if game:
            return game
    return None


def context() -> str:
    """Що зараз відбувається, для системного промпту."""
    bits = []
    if _vibe["game"]:
        mins = int((time.time() - _vibe["game_since"]) / 60)
        bits.append(f"Зараз Влад грає в {_vibe['game']} ({mins} хв).")
    if _vibe["track"]:
        bits.append(f"У Spotify грає «{_vibe['track']}» від {_vibe['artist']}.")
    return (" ЩО ЗАРАЗ: " + " ".join(bits)) if bits else ""


def _can_talk() -> bool:
    if not cfg.VIBE_ENABLED or timers.FOCUS_ACTIVE or tts._tts_active.is_set():
        return False
    if runtime.LIN_UI is not None and getattr(runtime.LIN_UI, "state", "idle") != "idle":
        return False
    now = time.time()
    _vibe["said"] = [t for t in _vibe["said"] if now - t < 3600]
    return len(_vibe["said"]) < cfg.VIBE_MAX_PER_HOUR


def _line(situation: str) -> str:
    """Коротка репліка від себе під ситуацію (без озвучки)."""
    from raphael import prompt          # prompt сам імпортує vibes
    resp = llm.llm_chat(
        cfg.GROQ_PRIMARY_MODEL,
        messages=prompt.inject_time([
            {"role": "system", "content": prompt.persona_block() + " "
             "Зараз ти сама, без запиту, вставляєш одну коротку репліку вголос: до 15 слів, "
             "без тегів дій, без лапок, без питань, на які треба відповідати."},
            {"role": "user", "content": situation},
        ]),
        max_tokens=400,
        temperature=1.0,
    )
    line = resp.choices[0].message.content or ""
    return llm._ACTION_RE.sub("", line).strip().strip('"«»')


def _say(situation: str) -> bool:
    """Озвучити репліку, якщо зараз можна. → чи сказала."""
    if not _can_talk():
        return False
    try:
        line = _line(situation)
    except Exception as e:
        log.warning(f"Вайби: модель не відповіла: {e}")
        return False
    if not line or not _can_talk():
        return False
    _vibe["said"].append(time.time())
    log.info(f"Вайби: {line}")
    from raphael import assistant       # assistant тягне prompt, а той vibes
    assistant.history.append({"role": "assistant", "content": line})
    tts.speak(line)
    return True


def music_tick():
    sp = music._get_spotipy()
    if not sp:
        return
    try:
        st = sp.current_playback()
    except Exception as e:
        log.debug(f"Вайби: Spotify: {e}")
        return
    if not st or not st.get("item") or not st.get("is_playing"):
        _vibe["track"] = None
        return
    track = st["item"]["name"]
    artist = ", ".join(a["name"] for a in st["item"]["artists"])
    if track == _vibe["track"]:
        return
    first = _vibe["track"] is None
    _vibe["track"], _vibe["artist"] = track, artist
    gap = time.time() - _vibe["music_last_quip"]
    if first or gap < cfg.VIBE_MUSIC_MIN_GAP_MIN * 60 or random.random() > cfg.VIBE_MUSIC_CHANCE:
        return
    where = f" Влад при цьому грає в {_vibe['game']}." if _vibe["game"] else ""
    if _say(f"Почався трек «{track}» від {artist}.{where} Кинь щось про вайб цього треку, як подруга, "
            f"що теж його слухає. Назву повністю не повторюй."):
        _vibe["music_last_quip"] = time.time()


def game_tick(game=None, now=None):
    game = detect_game() if game is None else game or None
    now = time.time() if now is None else now
    cur = _vibe["game"]
    if game and game != cur:
        _vibe.update(game=game, game_since=now, game_last_quip=now)
        log.info(f"Вайби: гра {game}")
        _say(f"Влад щойно запустив {game}. Кинь коротку репліку на старт гри.")
    elif not game and cur:
        mins = int((now - _vibe["game_since"]) / 60)
        _vibe["game"] = None
        log.info(f"Вайби: гру {cur} закрито через {mins} хв")
        if mins >= 10:
            _say(f"Влад закрив {cur} після {mins} хвилин гри. Прокоментуй.")
    elif game and now - _vibe["game_last_quip"] > cfg.VIBE_GAME_EVERY_MIN * 60:
        mins = int((now - _vibe["game_since"]) / 60)
        if _say(f"Влад грає в {game} вже {mins} хвилин. Підбадьор або підколи, ніби дивишся через плече."):
            _vibe["game_last_quip"] = now


def vibe_loop():
    log.info("Vibe loop запущено")
    time.sleep(60)          # дати привітанню відзвучати
    while True:
        try:
            if cfg.VIBE_ENABLED:
                game_tick()
                music_tick()
        except Exception as e:
            log.error(f"Вайби помилка: {e}")
        time.sleep(cfg.VIBE_INTERVAL)
