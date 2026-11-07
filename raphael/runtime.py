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
