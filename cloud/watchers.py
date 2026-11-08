"""
Вартові (етап 3): регулярні доручення за розкладом. «Щодня о 9:00 нові вакансії Python у
Вільнюсі», «кожні 6 годин знижки на ігри зі списку». Кожен запуск це звичайна справа агента
(cloud/agent.py) з памʼяттю про вже бачене: про старе мовчить, пише лише про нове.

Розклад людськими словами: «щодня 09:00», «щоранку», «щовечора», «кожні 6 год», «щогодини».
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from cloud import tools

NOTHING_NEW = "нового нема"
SEEN_KEEP = 300             # стільки посилань вартовий памʼятає
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def parse_schedule(text: str):
    """→ (розклад, решта тексту) або (None, текст). Розклад: {"daily": "09:00"} чи {"hours": 6}."""
    t = (text or "").strip()
    rules = [
        (r"^(щодня|кожен день|кожного дня)\s+(о\s+)?(\d{1,2})[:.](\d{2})\b", lambda m: {"daily": f"{int(m[3]):02d}:{m[4]}"}),
        (r"^(щоранку|щоранок|кожного ранку)\b", lambda m: {"daily": "09:00"}),
        (r"^(щовечора|кожного вечора)\b", lambda m: {"daily": "20:00"}),
        (r"^(щогодини|кожну годину)\b", lambda m: {"hours": 1}),
        (r"^кожн[іиа]\s+(\d{1,2})\s*(год|годин[иу]?|h)\b\.?", lambda m: {"hours": max(1, int(m[1]))}),
        (r"^(щодня|кожен день|кожного дня)\b", lambda m: {"daily": "09:00"}),
    ]
    for pattern, make in rules:
        m = re.match(pattern, t, re.I)
        if m:
            return make(m), t[m.end():].strip(" ,:-")
    return None, t


def describe(schedule: dict) -> str:
    if "daily" in schedule:
        return f"щодня о {schedule['daily']}"
    h = schedule.get("hours", 24)
    return "щогодини" if h == 1 else f"кожні {h} год"


def next_run(schedule: dict, after: datetime) -> datetime:
    if "daily" in schedule:
        hh, mm = map(int, schedule["daily"].split(":"))
        at = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return at if at > after else at + timedelta(days=1)
    return after + timedelta(hours=schedule.get("hours", 24))


class WatchStore:
    def __init__(self, path: str):
        self.path, self.lock = path, threading.RLock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"next_id": 1, "watches": {}}

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, goal: str, schedule: dict) -> dict:
        with self.lock:
            data = self._load()
            w = {"id": data["next_id"], "goal": goal.strip(), "schedule": schedule, "seen": [],
                 "next": next_run(schedule, tools.now()).isoformat(), "runs": 0, "found": 0, "created": time.time()}
            data["watches"][str(w["id"])] = w
            data["next_id"] += 1
            self._save(data)
            return w

    def get(self, wid) -> dict:
        with self.lock:
            return self._load()["watches"].get(str(wid))

    def update(self, wid, **fields) -> dict:
        with self.lock:
            data = self._load()
            w = data["watches"].get(str(wid))
            if w is not None:
                w.update(fields)
                self._save(data)
            return w

    def remove(self, wid) -> bool:
        with self.lock:
            data = self._load()
            gone = data["watches"].pop(str(wid), None) is not None
            self._save(data)
            return gone

    def all(self) -> list:
        with self.lock:
            return sorted(self._load()["watches"].values(), key=lambda w: w["id"])

    def due(self, at: datetime = None) -> list:
        """Ті, чий час настав; наступний запуск одразу переноситься, щоб не стартувати двічі."""
        at = at or tools.now()
        ready = []
        with self.lock:
            data = self._load()
            for w in data["watches"].values():
                if datetime.fromisoformat(w["next"]) <= at:
                    w["next"] = next_run(w["schedule"], at).isoformat()
                    w["runs"] += 1
                    ready.append(dict(w))
            if ready:
                self._save(data)
        return ready

    def remember(self, wid, report: str) -> list:
        """Нові посилання зі звіту → у памʼять вартового. Повертає ті, яких ще не бачив."""
        with self.lock:
            data = self._load()
            w = data["watches"].get(str(wid))
            if w is None:
                return []
            links = [u.rstrip(".,;") for u in URL_RE.findall(report or "")]
            fresh = [u for u in dict.fromkeys(links) if u not in w["seen"]]
            w["seen"] = (w["seen"] + fresh)[-SEEN_KEEP:]
            w["found"] += len(fresh)
            self._save(data)
            return fresh


def task_goal(watch: dict) -> str:
    """Мета для агента на один запуск вартового: з тим, що вже бачено."""
    seen = "\n".join(watch["seen"][-40:]) or "поки нічого"
    return (f"Регулярна перевірка ({describe(watch['schedule'])}): {watch['goal']}\n"
            "Шукай саме нове з минулої перевірки. Уже бачене (ці посилання не повторюй):\n"
            f"{seen}\n"
            f"Якщо нового нема, закінчи finish з текстом рівно «{NOTHING_NEW}». Якщо є: коротко про кожне нове, "
            "з посиланням.")


def listing(watches: list) -> str:
    if not watches:
        return ("Вартових нема. Постав: /watch щодня 09:00 нові вакансії Python у Вільнюсі "
                "(або «щоранку», «кожні 6 год»).")
    out = ["Вартові:"]
    for w in watches:
        when = datetime.fromisoformat(w["next"])
        out.append(f"#{w['id']} {describe(w['schedule'])}: {w['goal'][:70]} "
                   f"(далі {when:%d.%m %H:%M}, знайдено {w['found']})")
    return "\n".join(out)
