"""
Доручення: справи, які Лін виконує сама, поки Влад зайнятий (етап 2 плану «Лін як агент»).

Зберігаються у файлі на сервері, тож переживають перезапуск служби: недороблену справу
агент продовжує з того кроку, де зупинився (історія кроків лежить разом із нею).

Статуси: new (чекає черги), working, waiting (питання до Влада), done, failed, cancelled.
"""
import json
import os
import threading
import time

ICONS = {"new": "🕐", "working": "⏳", "waiting": "❓", "done": "✅", "failed": "⚠️", "cancelled": "✖️"}
WORDS = {"new": "у черзі", "working": "роблю", "waiting": "чекаю тебе", "done": "готово",
         "failed": "не вийшло", "cancelled": "скасовано"}
KEEP_FINISHED = 30          # стільки завершених справ лишається у файлі


class TaskStore:
    def __init__(self, path: str):
        self.path, self.lock = path, threading.RLock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"next_id": 1, "tasks": {}}

    def _save(self, data: dict) -> None:
        finished = [t for t in data["tasks"].values() if t["status"] in ("done", "failed", "cancelled")]
        for old in sorted(finished, key=lambda t: t["updated"])[:-KEEP_FINISHED]:
            data["tasks"].pop(str(old["id"]), None)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, goal: str, source: str = "telegram", watch_id=None) -> dict:
        with self.lock:
            data = self._load()
            task = {"id": data["next_id"], "goal": goal.strip(), "status": "new", "source": source,
                    "created": time.time(), "updated": time.time(), "steps": [], "messages": [],
                    "question": "", "question_msg": None, "pending_call": None, "result": "",
                    "watch_id": watch_id}
            data["tasks"][str(task["id"])] = task
            data["next_id"] += 1
            self._save(data)
            return task

    def get(self, task_id) -> dict:
        with self.lock:
            return self._load()["tasks"].get(str(task_id))

    def update(self, task_id, **fields) -> dict:
        with self.lock:
            data = self._load()
            task = data["tasks"].get(str(task_id))
            if task is None:
                return None
            task.update(fields, updated=time.time())
            self._save(data)
            return task

    def all(self) -> list:
        with self.lock:
            return sorted(self._load()["tasks"].values(), key=lambda t: -t["id"])

    def by_question(self, message_id) -> dict:
        """Справа, що чекає відповіді на це питання в Telegram (Влад відповів реплаєм)."""
        return next((t for t in self.all() if t["status"] == "waiting" and t.get("question_msg") == message_id), None)

    def unfinished(self) -> list:
        return [t for t in self.all() if t["status"] in ("new", "working")]


def line(task: dict) -> str:
    goal = task["goal"] if len(task["goal"]) <= 70 else task["goal"][:67] + "..."
    extra = f" (крок {len(task['steps'])})" if task["status"] == "working" and task["steps"] else ""
    return f"#{task['id']} {ICONS.get(task['status'], '')} {WORDS.get(task['status'], task['status'])}{extra}: {goal}"


def listing(tasks: list, limit: int = 10) -> str:
    # Завершені запуски вартових у списку справ лише заважали б: їх видно в /watches
    tasks = [t for t in tasks if not (t.get("watch_id") and t["status"] in ("done", "cancelled"))]
    if not tasks:
        return "Доручень нема. Дай справу: /task і що зробити, або просто «доручаю тобі ...»."
    return "Справи:\n" + "\n".join(line(t) for t in tasks[:limit])


def details(task: dict) -> str:
    if not task:
        return "Такої справи нема."
    out = [line(task)]
    for s in task["steps"][-8:]:
        out.append(f"  • {s}")
    if task["status"] == "waiting" and task["question"]:
        out.append(f"Питання: {task['question']}")
    if task["result"]:
        out.append("\n" + task["result"])
    return "\n".join(out)
