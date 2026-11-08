"""
Міст до ПК (етап 4): справи для домашнього компʼютера від агента на сервері.

Агент (cloud/agent.py) викликає інструмент pc_*, справа стає на паузу (waiting_pc), а
завдання лягає в цю чергу. Домашня Лін забирає черговий шматок разом з повідомленнями
(GET /home/updates через тунель), виконує його (raphael/pc_bridge.py, там і гальма: що
можна самій, що лише після кнопки «Так», що ніколи) і віддає результат (POST /home/jobs).
Результат продовжує справу з того самого кроку.

Компʼютер вимкнений: завдання просто чекає в файлі, хоч добу, і йде додому, щойно домашня
Лін вийде на звʼязок. Надіслане, на яке відповіді нема RESEND секунд (ПК вимкнувся посеред
нього), йде вдруге; домашня не виконує те саме двічі за один запуск.
"""
import json
import os
import threading
import time

RESEND = 180                # с без відповіді на надіслане: послати ще раз
KEEP_DONE = 50              # стільки виконаних лишається у файлі (для /show)

TOOLS = [
    {"type": "function", "function": {
        "name": "pc_look", "description": "Глянути на екран домашнього ПК Влада і відповісти на питання про "
                                          "побачене. Перед тим Влад підтверджує кнопкою в Telegram.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "pc_read_page", "description": "Відкрити сторінку з домашнього компʼютера, коли з сервера вона не "
                                               "відкривається (LinkedIn відповідає 429, сайт не пускає хмару).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "pc_note", "description": "Записати нотатку у «Вхідні» сховища Obsidian Влада на ПК: лише дописує, "
                                          "нічого не стирає й не переписує.",
        "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
                       "required": ["title", "text"]}}},
    {"type": "function", "function": {
        "name": "pc_say", "description": "Сказати Владу вголос удома, голосом Лін з компʼютера.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
]
NAMES = {t["function"]["name"] for t in TOOLS}


def describe(tool: str, args: dict) -> str:
    """Що саме просять зробити на ПК, людською мовою (для кроків справи й кнопки «Так / Ні»)."""
    args = args or {}
    text = {"pc_look": f"глянути на екран: {args.get('question', '')}",
            "pc_read_page": f"відкрити з дому {args.get('url', '')}",
            "pc_note": f"записати в Obsidian «{args.get('title', '')}»",
            "pc_say": f"сказати вголос: {args.get('text', '')}",
            "pc_page_text": f"прочитати для вартового {args.get('url', '')}",
            "pc_page_links": f"взяти посилання для вартового {args.get('url', '')}"}.get(tool, tool)
    return text.strip()[:160]


class PcJobs:
    """Черга завдань для ПК у файлі: переживає перезапуск служби й вимкнений компʼютер."""

    def __init__(self, path: str, clock=time.time):
        self.path, self.clock, self.lock = path, clock, threading.RLock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"next_id": 1, "jobs": {}}

    def _save(self, data: dict) -> None:
        done = [j for j in data["jobs"].values() if j["status"] in ("done", "cancelled")]
        for old in sorted(done, key=lambda j: j["created"])[:-KEEP_DONE]:
            data["jobs"].pop(str(old["id"]), None)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, task_id, call_id: str, tool: str, args: dict) -> dict:
        with self.lock:
            data = self._load()
            job = {"id": data["next_id"], "task_id": task_id, "call_id": call_id, "tool": tool, "args": args or {},
                   "status": "queued", "created": self.clock(), "sent": 0, "result": ""}
            data["jobs"][str(job["id"])] = job
            data["next_id"] += 1
            self._save(data)
            return job

    def get(self, job_id) -> dict:
        with self.lock:
            return self._load()["jobs"].get(str(job_id))

    def has_pending(self) -> bool:
        """Чи є що віддати домашній просто зараз (тоді її запит не тримаємо)."""
        now = self.clock()
        with self.lock:
            return any(j["status"] == "queued" or (j["status"] == "sent" and now - j["sent"] >= RESEND)
                       for j in self._load()["jobs"].values())

    def for_home(self) -> list:
        """Завдання для домашньої: нові й ті, на які надто довго нема відповіді. Позначаються надісланими."""
        now, out = self.clock(), []
        with self.lock:
            data = self._load()
            for j in data["jobs"].values():
                if j["status"] == "queued" or (j["status"] == "sent" and now - j["sent"] >= RESEND):
                    j["status"], j["sent"] = "sent", now
                    out.append({"id": j["id"], "task_id": j["task_id"], "tool": j["tool"], "args": j["args"]})
            if out:
                self._save(data)
        return sorted(out, key=lambda j: j["id"])

    def unsend(self, ids: list) -> None:
        """Віддати не вийшло (тунель упав саме тоді): назад у чергу."""
        with self.lock:
            data = self._load()
            for i in ids:
                j = data["jobs"].get(str(i))
                if j and j["status"] == "sent":
                    j["status"] = "queued"
            self._save(data)

    def finish(self, job_id, result: str):
        """Результат з дому → завдання, якщо воно ще чекало, інакше None (повтор чи скасоване)."""
        with self.lock:
            data = self._load()
            j = data["jobs"].get(str(job_id))
            if not j or j["status"] in ("done", "cancelled"):
                return None
            j["status"], j["result"] = "done", str(result or "")[:60000]
            self._save(data)
            return dict(j)

    def cancel(self, job_id) -> None:
        with self.lock:
            data = self._load()
            j = data["jobs"].get(str(job_id))
            if j and j["status"] in ("queued", "sent"):
                j["status"] = "cancelled"
                self._save(data)

    def cancel_task(self, task_id) -> int:
        with self.lock:
            data = self._load()
            hit = 0
            for j in data["jobs"].values():
                if str(j["task_id"]) == str(task_id) and j["status"] in ("queued", "sent"):
                    j["status"] = "cancelled"
                    hit += 1
            if hit:
                self._save(data)
            return hit

    def waiting(self) -> list:
        with self.lock:
            return [j for j in self._load()["jobs"].values() if j["status"] in ("queued", "sent")]
