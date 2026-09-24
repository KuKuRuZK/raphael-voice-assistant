"""
Гарячі клавіші: push-to-talk, тап правим Ctrl, диктовка у сховище.
"""
import logging
import threading
import time

from raphael import dictation
from raphael import settings as cfg
from raphael import tts

log = logging.getLogger("Лін")

try:
    import keyboard as _keyboard   # глобальні гарячі клавіші (push-to-talk)
except Exception:
    _keyboard = None


_ptt_event = threading.Event()     # ставиться коли натиснуто клавішу активації


def _toggle_ptt(on: bool = None) -> None:
    """Перемикає режим push-to-talk."""
    cfg.PUSH_TO_TALK = (not cfg.PUSH_TO_TALK) if on is None else on
    _ptt_event.clear()
    if cfg.PUSH_TO_TALK:
        _key = cfg.TAP_TOGGLE_KEY if (cfg.TAP_TOGGLE_KEY and cfg.TAP_ACTION == "ptt") else cfg.PTT_HOTKEY
        tts.speak(f"Режим кнопки. Слухаю після {_key.replace('+', ' ').replace('right ctrl', 'правого контролу')}.")
        log.info("PTT увімкнено")
    else:
        tts.speak("Постійне слухання. Кажи моє ім'я як завжди.")
        log.info("PTT вимкнено")


# Прапорець тримаємо окремо від часу: час натискання може бути будь-яким
# числом, і перевіряти його на істинність не можна.
_tap_pressed = False
_tap_down_at = 0.0
_tap_combo   = False


def _tap_watcher(e) -> None:
    """
    Перемикає режим кнопки коротким тапом по одній клавіші.
    Клавіша лишається повноцінним модифікатором: якщо разом з нею натиснули
    щось іще або тримали довше за TAP_MAX_HOLD — перемикач не спрацьовує.
    """
    global _tap_pressed, _tap_down_at, _tap_combo
    try:
        if e.name == cfg.TAP_TOGGLE_KEY:
            if e.event_type == "down":
                if not _tap_pressed:            # ігноруємо автоповтор утримання
                    _tap_pressed = True
                    _tap_down_at = time.monotonic()
                    _tap_combo = False
            else:
                held = time.monotonic() - _tap_down_at if _tap_pressed else 99.0
                combo = _tap_combo
                _tap_pressed, _tap_combo = False, False
                if not combo and held < cfg.TAP_MAX_HOLD:
                    if cfg.TAP_ACTION == "toggle":
                        log.info(f"Тап по {cfg.TAP_TOGGLE_KEY}: перемикаю режим кнопки")
                        _toggle_ptt()
                    elif cfg.TAP_ACTION == "dictate":
                        log.info(f"Тап по {cfg.TAP_TOGGLE_KEY}: диктовка у мозок")
                        dictation._dictate_to_brain()
                    else:
                        log.info(f"Тап по {cfg.TAP_TOGGLE_KEY}: активація, слухаю команду")
                        _ptt_event.set()
        elif e.event_type == "down" and _tap_pressed:
            _tap_combo = True                   # використали як модифікатор
    except Exception as ex:
        log.error(f"Тап-перемикач: {ex}")


def _setup_hotkeys() -> None:
    """Реєструє глобальні гарячі клавіші (push-to-talk + диктовка у мозок)."""
    if _keyboard is None:
        log.warning("keyboard не доступний — гарячі клавіші вимкнені")
        return
    try:
        _keyboard.add_hotkey(cfg.PTT_HOTKEY, lambda: _ptt_event.set())
        _keyboard.add_hotkey(cfg.PTT_TOGGLE_HOTKEY, lambda: _toggle_ptt())
        log.info(f"Гарячі клавіші: {cfg.PTT_HOTKEY} (активація), {cfg.PTT_TOGGLE_HOTKEY} (режим)")
    except Exception as e:
        log.error(f"Не вдалося зареєструвати гарячі клавіші: {e}")

    # Диктовку реєструємо окремо: якщо саме ця комбінація зайнята іншою програмою,
    # решта гарячих клавіш має лишитись робочою.
    try:
        _keyboard.add_hotkey(cfg.BRAIN_HOTKEY, dictation._dictate_to_brain)
        log.info(f"Гаряча клавіша диктовки у мозок: {cfg.BRAIN_HOTKEY}")
    except Exception as e:
        log.error(f"Не вдалося зареєструвати {cfg.BRAIN_HOTKEY} для диктовки: {e}")

    if cfg.TAP_TOGGLE_KEY:
        try:
            _keyboard.hook(_tap_watcher)
            what = {"toggle": "режим кнопки", "dictate": "диктовка у мозок"}.get(cfg.TAP_ACTION, "активація команди")
            log.info(f"Тап по {cfg.TAP_TOGGLE_KEY} → {what} (коротке натискання окремо)")
        except Exception as e:
            log.error(f"Не вдалося повісити тап-перемикач на {cfg.TAP_TOGGLE_KEY}: {e}")
