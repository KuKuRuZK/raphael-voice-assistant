"""
Диктовка: у поле під курсором і у Вхідні сховища Obsidian.
"""
import logging
import threading
import time

import pyautogui

from raphael import brain
from raphael import runtime
from raphael import settings as cfg
from raphael import stt
from raphael import tts

log = logging.getLogger("Лін")

try:
    import keyboard as _keyboard   # глобальні гарячі клавіші (push-to-talk)
except Exception:
    _keyboard = None


_brain_dictating = threading.Event()   # диктовка триває
_brain_stop      = threading.Event()   # друге натискання клавіші = завершити
_brain_started_at   = 0.0              # коли почалась поточна диктовка
_brain_last_trigger = 0.0              # коли востаннє спрацювала гаряча клавіша
BRAIN_DEBOUNCE    = 0.6                # ігнорувати повторні спрацювання частіше за це
BRAIN_MIN_SESSION = 3.0                # перші секунди диктовку не зупиняємо


def _dictate_to_brain() -> None:
    """
    Диктовка одразу у Вхідні другого мозку, без звертання на ім'я і без LLM.
    Що сказав — те й записалось, дослівно.

    Перше натискання починає, друге завершує. Довгі паузи між реченнями не
    обривають запис: на час диктовки поріг паузи піднімається до
    BRAIN_PAUSE_THRESHOLD, а шматки склеюються в ОДИН рядок.
    """
    global _brain_started_at, _brain_last_trigger
    now = time.monotonic()

    # Автоповтор клавіатури: утримана клавіша шле десятки натискань за секунду.
    # Без цього фільтра повтори миттєво зупиняли щойно розпочату диктовку.
    if now - _brain_last_trigger < BRAIN_DEBOUNCE:
        return
    _brain_last_trigger = now

    if _brain_dictating.is_set():
        # Захист від того самого автоповтору: перші секунди диктовку не зупиняємо.
        if now - _brain_started_at < BRAIN_MIN_SESSION:
            log.debug("Brain dictate: ранній сигнал завершити проігноровано")
            return
        _brain_stop.set()
        log.info("Brain dictate: отримано сигнал завершити")
        return

    _brain_dictating.set()
    _brain_stop.clear()
    _brain_started_at = now

    def _run():
        try:
            text = _capture_dictation("Диктуй. Скажи стоп, коли договориш.", _brain_stop)
            if not text:
                tts.speak("Нічого не розчула.")
                return
            result = brain.brain_capture(text)
            log.info(f"Brain dictate: {len(text)} символів")
            tts.speak(result if len(text) < 120 else "Записала.")
        except Exception as e:
            log.error(f"Brain dictate помилка: {e}", exc_info=True)
            tts.speak("Щось пішло не так із диктовкою.")
        finally:
            _brain_dictating.clear()
            _brain_stop.clear()
            if runtime.LIN_UI: runtime.LIN_UI.safe_set_state("idle")

    threading.Thread(target=_run, daemon=True).start()


_typing_dictation = threading.Event()
_last_dictation = ""      # остання диктовка під курсор — щоб не втратити її назавжди

# Слова, якими можна завершити диктовку голосом. Саме слово в текст не потрапляє.
_DICTATE_STOP_WORDS = ("кінець диктовки", "кінець запису", "стоп запис",
                       "кінець", "стоп", "досить", "все стоп")


def _strip_stop_word(text: str):
    """Повертає (текст без стоп-слова, чи було стоп-слово)."""
    t = text.strip()
    low = t.lower().rstrip(" .,!?")
    # Від найдовшого: інакше «все стоп» збігається з «стоп» і лишає «все».
    for w in sorted(_DICTATE_STOP_WORDS, key=len, reverse=True):
        if low.endswith(w):
            cut = len(low) - len(w)
            return t[:cut].rstrip(" .,!?—-"), True
    return t, False


def _capture_dictation(intro: str, stop_event=None) -> str:
    """
    Спільний збір диктовки: піднімає поріг паузи, накопичує фрагменти
    і завершується за стоп-словом, тишею або зовнішньою подією.
    """
    old_pause = getattr(stt.recognizer, "pause_threshold", 0.9)
    chunks = []
    try:
        stt.recognizer.pause_threshold = cfg.BRAIN_PAUSE_THRESHOLD
        if intro:
            tts.speak(intro)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            part = stt.listen(timeout=10, phrase_limit=cfg.BRAIN_DICTATE_LIMIT)
            if not part:
                break                       # тиша — вважаємо, що договорив
            cleaned, stopped = _strip_stop_word(part)
            if cleaned:
                chunks.append(cleaned)
            if stopped:
                log.info("Диктовка завершена стоп-словом")
                break
            if stop_event is not None and stop_event.is_set():
                break
    finally:
        try:
            stt.recognizer.pause_threshold = old_pause
        except Exception:
            pass
    return " ".join(chunks).strip()


def _type_at_cursor(text: str) -> None:
    """Вставляє готовий текст туди, де курсор. Без диктовки."""
    global _last_dictation
    text = (text or "").strip()
    if not text:
        tts.speak("А що друкувати?")
        return
    _last_dictation = text
    try:
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.08)
        try:
            if _keyboard is not None:
                _keyboard.send("ctrl+v")
            else:
                pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.hotkey("ctrl", "v")
        log.info(f"Type at cursor: {text}")
        tts.speak("Надрукувала. Текст у буфері.")
    except Exception as e:
        log.error(f"Type at cursor: {e}")
        tts.speak("Не вийшло надрукувати.")


def _dictate_to_cursor() -> None:
    """
    Диктовка, яка друкується туди, де стоїть курсор.
    Друкуємо вставкою з буфера, а не емуляцією клавіш: емуляція на кирилиці
    залежить від активної розкладки, вставка — ні. Попередній вміст буфера
    повертаємо назад.
    """
    if _typing_dictation.is_set():
        log.info("Dictate to cursor: вже слухаю")
        tts.speak("Вже слухаю, диктуй.")
        return
    _typing_dictation.set()

    def _run():
        global _last_dictation
        try:
            text = _capture_dictation("Диктуй, друкую. Скажи стоп, коли договориш.")
            if not text:
                tts.speak("Нічого не розчула.")
                return
            # Зберігаємо ДО вставки: якщо фокус не на текстовому полі, Ctrl+V
            # нікуди не потрапить, і без цієї копії текст загине безслідно.
            _last_dictation = text
            log.info(f"Dictate to cursor (текст): {text}")

            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.08)
            try:
                if _keyboard is not None:
                    _keyboard.send("ctrl+v")      # по скан-кодах, не залежить від розкладки
                else:
                    pyautogui.hotkey("ctrl", "v")
            except Exception:
                pyautogui.hotkey("ctrl", "v")

            # Старий буфер НЕ повертаємо. Раніше повертали через 0.4 с — і якщо
            # фокус був не на текстовому полі, вставка нікуди не потрапляла, а
            # відновлення буфера знищувало єдину копію надиктованого.
            # Тепер текст лишається в буфері: не вставилось — просто тисни Ctrl+V.
            tts.speak("Надрукувала. Текст у буфері.")
        except Exception as e:
            log.error(f"Dictate to cursor помилка: {e}", exc_info=True)
            tts.speak("Не вийшло надрукувати.")
        finally:
            _typing_dictation.clear()
            if runtime.LIN_UI: runtime.LIN_UI.safe_set_state("idle")

    threading.Thread(target=_run, daemon=True).start()
