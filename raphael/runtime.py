"""
Спільний стан процесу, потрібний багатьом модулям: вікно (LIN_UI),
прапорець дії, що сама повідомила про невдачу, маркер свідомого виходу.
"""
import threading

from raphael import settings as cfg


LIN_UI = None   # встановлюється в main()

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
