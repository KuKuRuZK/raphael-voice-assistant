"""
Тіло: аватар Лін поверх робочого столу (Electron, тека AVATAR_DIR).

Тут мозок каже тілу, що відбувається:
  • режим: сама бачить гру; у грі монітор, де стоїть вікно гри, для неї закритий;
  • думка (mind): слухає, думає, говорить (текст речення і гучність звуку
    12 разів на секунду, щоб рот рухався в такт), чекає;
  • музика (vibe): що грає в Spotify;
  • тіло на прохання (body): «потанцюй», «сядь на вікно» → тег avatar:дія.
Міст: POST на AVATAR_URL (/state, /mind, /vibe, /do); тіла нема, то тихо нічого.
Думка, музика й дії йдуть через чергу в окремому потоці: озвучка й вікно не чекають мережу.
Стан шлеться раз на AVATAR_INTERVAL навіть без змін: тіло, перезапущене окремо,
за кілька секунд знає, що гра йде. Гра визначається так само, як у вайбах
(vibes.game_from_process), але незалежно від VIBE_ENABLED.

Від гри залежить і мозок (runtime.GAME → llm.main_model): у грі хмара, а локальна
модель вивантажується, щоб гра мала всю відеопамʼять; гра скінчилась, і вона
вантажиться знову. Це працює, навіть коли тіло вимкнене (AVATAR_ENABLED).
"""
import ctypes
import ctypes.wintypes
import json
import logging
import os
import subprocess
import queue
import threading
import time
import urllib.request

import psutil

from raphael import llm
from raphael import runtime
from raphael import settings as cfg
from raphael import vibes

log = logging.getLogger("Лін")

_last = {"mode": None, "game": None, "rect": None, "online": None, "body": {}}


def post_json(path: str, data: dict, timeout: float = 0.5):
    """Надіслати тілу подію. Його відповідь (dict) або None, якщо не відповіло."""
    if not cfg.AVATAR_ENABLED:
        return None
    try:
        req = urllib.request.Request(cfg.AVATAR_URL.rstrip("/") + path, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}") if r.status == 200 else None
    except Exception:
        return None


def post(path: str, data: dict, timeout: float = 0.5) -> bool:
    """Надіслати тілу подію. True, якщо воно відповіло."""
    return post_json(path, data, timeout) is not None


# ── Меню трею: тілом керує трей мозку, значок один ───────────────────────────
def online() -> bool:
    return bool(_last["online"])


def body_state(key):
    """Тумблер тіла з останньої відповіді (autopilot, manual, aura, skin)."""
    return _last["body"].get(key)


def set_body(**kw) -> bool:
    """Змінити тумблер тіла з меню; відповідь одразу оновлює галочки."""
    r = post_json("/set", kw, timeout=1.0)
    if r:
        _last["body"].update({k: r[k] for k in ("autopilot", "manual", "aura", "skin") if k in r})
    return r is not None


def quit_body():
    post("/quit", {}, timeout=1.0)


# ── Черга подій тілу ─────────────────────────────────────────────────────────
_queue = queue.Queue(maxsize=64)
_sender = None


def _send_loop():
    while True:
        path, data = _queue.get()
        post(path, data, timeout=0.3)


def send(path: str, data: dict):
    """Покласти подію в чергу (не блокує). Черга повна, тобто тіло зависло: стару подію викинути."""
    global _sender
    if not cfg.AVATAR_ENABLED:
        return
    if _sender is None:
        _sender = threading.Thread(target=_send_loop, daemon=True)
        _sender.start()
    try:
        _queue.put_nowait((path, data))
    except queue.Full:
        try:
            _queue.get_nowait()
        except queue.Empty:
            pass
        _queue.put_nowait((path, data))


def levels_of(path: str, per_sec: int = 12):
    """Гучність звуку DRAWINGS разів на секунду (0..1), щоб рот відкривався в такт.
    None, якщо файл не прочитався: тоді тіло рухає ротом за складами."""
    try:
        import numpy as np
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        freq, _, channels = pygame.mixer.get_init()
        a = pygame.sndarray.array(pygame.mixer.Sound(path)).astype(np.float32)
        if a.ndim > 1:
            a = np.abs(a).mean(axis=1)
        step = max(1, int(freq / per_sec))
        n = len(a) // step
        if n == 0:
            return None
        rms = np.sqrt((a[:n * step].reshape(n, step) ** 2).mean(axis=1))
        top = np.percentile(rms, 95) or 1.0
        return [round(float(min(1.0, v / top)), 2) for v in rms]
    except Exception as e:
        log.debug(f"Аватар: гучність не порахована: {e}")
        return None


def mind(state: str, text: str = "", path: str = None, emo: str = None):
    """Що зараз робить голова: listening, thinking, speaking (з текстом речення
    і настроєм відповіді, raphael.emotion), idle."""
    data = {"state": state, "text": (text or "")[:300]}
    if state == "speaking" and path:
        data["levels"] = levels_of(path)
    if state == "speaking" and emo:
        data["emo"] = emo
    send("/mind", data)


def vibe(playing: bool, track: str = None, artist: str = None):
    send("/vibe", {"music": playing, "track": track, "artist": artist})


# Що тіло вміє на прохання (тег avatar:дія); решта дій тіла лише для автопілота
BODY = {"dance", "sitA", "peekB", "music", "guitar", "chill", "sleep", "knock", "travel", "tp",
        "laugh", "stretch", "bubble", "tantrum", "hang", "photo", "climb", "point", "holo", "cursor", "type"}


def body(action: str) -> bool:
    """Дія тіла на прохання. False, якщо такої нема."""
    action = (action or "").strip()
    if action not in BODY:
        log.warning(f"Аватар: тіло не вміє «{action}»")
        return False
    send("/do", {"action": action})
    return True


def find_game():
    """→ (назва гри, [pid усіх її процесів]) або (None, []). Лаунчер і сама гра часто окремі процеси."""
    game, pids = None, []
    for proc in psutil.process_iter(["name", "exe", "pid"]):
        try:
            g = vibes.game_from_process(proc.info.get("name"), proc.info.get("exe"))
        except Exception:
            continue
        if g and (game is None or g == game):
            game = g
            pids.append(proc.info["pid"])
    return game, pids


def window_rect(pids):
    """Найбільше видиме не згорнуте вікно цих процесів → [x, y, w, h] або None."""
    user32 = ctypes.windll.user32
    want = set(pids)
    best = {"rect": None, "area": 0}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def each(hwnd, _):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in want and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best["area"]:
                best.update(rect=[r.left, r.top, r.right - r.left, r.bottom - r.top], area=area)
        return True

    user32.EnumWindows(each, 0)
    return best["rect"]


def tick(game=None, pids=None):
    """Один крок: що зараз (гра чи ні, де її вікно) → тілу. Повертає надіслане."""
    if game is None and pids is None:
        game, pids = find_game()
    rect = window_rect(pids) if game and pids else None
    if game and rect is None and _last["game"] == game:
        rect = _last["rect"]                    # згорнута чи alt-tab: монітор той самий
    data = {"mode": "game" if game else "normal", "game": game, "rect": rect}
    reply = post_json("/state", data)
    online = reply is not None
    if reply:
        _last["body"] = {k: reply[k] for k in ("autopilot", "manual", "aura", "skin") if k in reply}
    runtime.GAME = game
    if data["mode"] != _last["mode"] or game != _last["game"]:
        log.info(f"Аватар: {'гра ' + game if game else 'звичайний режим'}"
                 f"{', вікно ' + str(rect) if rect else ''}, мозок {llm.main_model()}")
    if data["mode"] != _last["mode"] and cfg.BRAIN_LOCAL_MODEL:
        threading.Thread(target=brain_switch, daemon=True).start()
    if online != _last["online"]:
        log.info(f"Аватар: тіло {'на звʼязку' if online else 'не відповідає'} ({cfg.AVATAR_URL})")
    _last.update(mode=data["mode"], game=game, rect=rect, online=online)   # body оновлено вище
    return data


_brain_lock = threading.Lock()


def brain_switch():
    """Привести локальну модель до режиму: у грі вивантажена, поза грою в памʼяті."""
    with _brain_lock:                            # завантаження довге: перемикання по черзі
        try:
            if runtime.GAME and cfg.BRAIN_FREE_IN_GAME:
                llm.local_unload()
            elif not runtime.GAME and cfg.BRAIN_PRELOAD:
                llm.local_preload()
                if runtime.GAME and cfg.BRAIN_FREE_IN_GAME:   # поки вантажилась, почалась гра
                    llm.local_unload()
        except Exception as e:
            log.error(f"Мозок: перемикання не вдалось: {e}")


def launch() -> bool:
    """Запустити тіло (Electron з AVATAR_DIR), якщо воно ще не відповідає."""
    if not cfg.AVATAR_ENABLED or not cfg.AVATAR_DIR or post("/state", {"mode": _last["mode"] or "normal"}):
        return False
    exe = os.path.join(cfg.AVATAR_DIR, "node_modules", "electron", "dist", "electron.exe")
    if not os.path.exists(exe):
        log.warning(f"Аватар: нема {exe}, у теці аватара треба npm install")
        return False
    subprocess.Popen([exe, cfg.AVATAR_DIR], cwd=cfg.AVATAR_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log.info(f"Аватар: запущено тіло з {cfg.AVATAR_DIR}")
    return True


def avatar_loop():
    if not (cfg.AVATAR_ENABLED or cfg.BRAIN_LOCAL_MODEL):
        return
    log.info("Аватар: міст запущено" if cfg.AVATAR_ENABLED else "Аватар вимкнено, стежу за грою для мозку")
    try:
        launch()
    except Exception as e:
        log.error(f"Аватар: не запустився: {e}")
    while True:
        try:
            tick()
        except Exception as e:
            log.error(f"Аватар помилка: {e}")
        time.sleep(cfg.AVATAR_INTERVAL)
