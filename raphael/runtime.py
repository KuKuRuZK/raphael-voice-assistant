"""
Спільний стан процесу, потрібний багатьом модулям: що Лін зараз робить (LIN_STATE),
прапорець дії, що сама повідомила про невдачу, маркер свідомого виходу.
"""
import threading

from raphael import settings as cfg


# Що Лін зараз робить: idle, listening, thinking, speaking. Показує це тіло на екрані,
# а vibes не вставляє реплік, поки стан не idle. Колись стан малювало ще окреме
# вікно з орбом (raphael/ui.py, orb.py), прибране 2026-11-07: його не було видно.
LIN_STATE = "idle"


def show_state(state: str, text: str = ""):
    """Новий стан з будь-якого потоку. «Говорю» тілу шле сама озвучка, з текстом
    речення, гучністю й настроєм, тож тут йому лише решта станів."""
    global LIN_STATE
    LIN_STATE = state
    if state != "speaking":
        from raphael import avatar          # avatar тягне llm і vibes: не на верху файлу
        avatar.mind(state, text)

# Де зараз Влад, тобто куди Лін говорить: "voice" (уголос удома) або "telegram"
# (у чат на телефоні). Повідомлення в Telegram перемикає на telegram, будь-яка
# голосова команда вдома назад на voice. Тоді все, що Лін каже (відповіді, погода,
# пошта, нагадування), іде туди, де Влад, а не в порожню кімнату.
LIN_CHANNEL = "voice"
TG_CHAT_ID = None        # чат Telegram, куди відповідати (ставить raphael.tg_lin)
REMOTE_SAY = None        # функція(текст): сказати в Telegram (ставить raphael.tg_lin)
REMOTE_CONFIRM = None    # функція(питання) → bool: кнопки «Так / Ні» в Telegram


def use_channel(channel: str):
    """Перемкнути, куди Лін говорить ("voice" чи "telegram")."""
    global LIN_CHANNEL
    LIN_CHANNEL = channel


def remote() -> bool:
    """Чи говорить Лін зараз у Telegram (і чи бот на звʼязку)."""
    return LIN_CHANNEL == "telegram" and REMOTE_SAY is not None


# Назва гри, поки вона йде (ставить raphael.avatar), інакше None. Від неї
# залежить мозок: у грі хмарна модель, поза грою локальна (llm.main_model).
GAME = None

# Дія, що сама сказала про невдачу («Не знайшла програму»), ставить прапорець,
# і ask_lin тоді не зачитує текст моделі («Відкриваю…»): він був би неправдою.
_action_failed = threading.Event()


def _mark_action_failed():
    _action_failed.set()


def _mark_stop():
    """Створює маркер свідомого виходу — щоб watchdog НЕ перезапускав Лін."""
    try:
        with open(cfg.STOP_MARKER, "w", encoding="utf-8") as f:
            f.write("stop")
    except Exception:
        pass
