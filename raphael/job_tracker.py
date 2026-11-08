"""
Трекер вакансій Влада (Job_Search/vacancies_master.csv): Лін лише дописує рядки.

CSV це джерело правди для /apply-jobs, pending.py і build_tracker.py: перший рядок
заголовок, 11 колонок (#, Компанія, Посада, Напрям, Локація, Зарплата/міс, Match%, Чому
підходить, ATS ключові слова, Статус, Лінк), UTF-8 з BOM, рядки через LF. Нова вакансія
отримує статус «Не подано», і /apply-jobs її підхоплює.

Що приходить у рядок, писала модель за текстом чужої сторінки, тож: усе в один рядок,
обрізане, і без = + - @ на початку (build_tracker.py кладе це в XLSX, а там такий текст
став би формулою). Шлях до файлу лише з налаштувань ПК, ніколи із завдання.
"""
import csv
import logging
import os
import re
import shutil
import urllib.parse
from datetime import datetime

from raphael import settings as cfg

log = logging.getLogger("Лін")

COLUMNS = 11
STATUS_NEW = "Не подано"


def _norm(url: str) -> str:
    """Та сама вакансія за різними адресами: без www./en., хвостів ?... і кінцевої косої."""
    u = urllib.parse.urlsplit((url or "").strip())
    host = re.sub(r"^(www|en|lt)\.", "", u.netloc.lower())
    return host + u.path.rstrip("/")


def _safe(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text.lstrip("=+-@\t ")[:limit]


def add(row: dict, path: str = None) -> str:
    """Дописати вакансію (row: url, company, title, area, place, salary, match, why) → що сказати Владу."""
    path = path or cfg.JOB_TRACKER_CSV
    url = (row.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        return "Без посилання на вакансію в трекер не додаю."
    if not os.path.isfile(path):
        return f"Трекер не знайшла: {path}"
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        if not rows or len(rows[0]) != COLUMNS:
            return "Трекер має незнайомий формат (не 11 колонок), нічого не чіпала."
        for r in rows[1:]:
            if len(r) >= COLUMNS and _norm(r[10]) == _norm(url):
                return f"Уже є в трекері: #{r[0]} {r[1]}, {r[2]} ({r[9]})."
        num = max((int(r[0]) for r in rows[1:] if r and r[0].strip().isdigit()), default=0) + 1
        match = row.get("match")
        new = [str(num), _safe(row.get("company"), 80), _safe(row.get("title"), 120), _safe(row.get("area"), 30),
               _safe(row.get("place"), 80), _safe(row.get("salary"), 60),
               str(match) if isinstance(match, int) else "", _safe(row.get("why"), 300), "", STATUS_NEW, url]
        backups = os.path.join(os.path.dirname(path), "backups")
        if os.path.isdir(backups):
            copy = os.path.join(backups, f"vacancies_master_{datetime.now():%Y-%m-%d}_before_lin.csv")
            if not os.path.exists(copy):
                shutil.copy2(path, copy)
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            last = b""
            if size:
                f.seek(-1, os.SEEK_END)
                last = f.read(1)
        ends_clean = size == 0 or last == b"\n"
        with open(path, "a", encoding="utf-8", newline="") as f:
            if not ends_clean:
                f.write("\n")
            csv.writer(f, lineterminator="\n").writerow(new)
    except PermissionError:
        return "Трекер зараз відкритий в іншій програмі (Excel?), не змогла дописати. Закрий і натисни ще раз."
    except (OSError, ValueError, csv.Error) as e:
        log.error(f"Трекер: {e}", exc_info=True)
        return f"Не вийшло дописати в трекер: {type(e).__name__}."
    log.info(f"Трекер: #{num} {new[1]}, {new[2]}")
    who = ", ".join(x for x in (new[1], new[2]) if x)
    return f"Додала в трекер #{num}: {who}. Статус «{STATUS_NEW}», /apply-jobs її побачить."
