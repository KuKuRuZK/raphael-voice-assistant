"""
Заглушки для Windows-only і апаратних бібліотек, щоб імпортувати Рафаеля на
будь-якій ОС (CI на Linux, пісочниця). Справжні лишаються там, де вони
легкі й кросплатформні: psutil, SpeechRecognition, spotipy, Pillow.

install() треба викликати ДО імпорту lin.
"""
import os
import sys
import tempfile
import types


class _Any:
    """Обʼєкт, що приймає будь-які виклики й атрибути (вікна, трей, клієнти)."""
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, name):
        return _Any()


def _module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Communicate:
    """edge_tts.Communicate без мережі: пише порожній mp3."""
    def __init__(self, *a, **k):
        pass

    async def save(self, path):
        with open(path, "wb") as f:
            f.write(b"ID3")


def install():
    # Логи, маркери й кеш озвучки не мають падати в робочу теку чи в %LOCALAPPDATA%
    os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp(prefix="raphael-test-"))

    music = types.SimpleNamespace(load=lambda p: None, play=lambda: None,
                                  get_busy=lambda: False, stop=lambda: None,
                                  unload=lambda: None)
    _module("pygame",
            mixer=types.SimpleNamespace(pre_init=lambda *a: None, init=lambda: None,
                                        get_init=lambda: True, music=music),
            time=types.SimpleNamespace(Clock=_Any))
    _module("pyautogui", FAILSAFE=True, hotkey=lambda *k: None, press=lambda k: None,
            screenshot=_Any)
    _module("pystray", Icon=_Any, Menu=_Any, MenuItem=_Any)
    _module("tkinter", Tk=_Any, Toplevel=_Any, Frame=_Any, Label=_Any, Canvas=_Any,
            StringVar=_Any)
    _module("tkinter.messagebox", showwarning=lambda *a, **k: None)
    _module("edge_tts", Communicate=_Communicate)
    _module("groq", Groq=_Any)
