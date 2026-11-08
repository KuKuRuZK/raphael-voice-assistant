"""
Вартові (етап 3): регулярні доручення за розкладом. «Щодня о 9:00 нові вакансії Python у
Вільнюсі», «кожні 6 годин знижки на ігри зі списку». Кожен запуск це звичайна справа агента
(cloud/agent.py) з памʼяттю про вже бачене: про старе мовчить, пише лише про нове.

Розклад людськими словами: «щодня 09:00», «щоранку», «щовечора», «кожні 6 год», «щогодини».
"""
import itertools
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

from cloud import tools

NOTHING_NEW = "нового нема"
SEEN_KEEP = 1500            # стільки посилань вартовий памʼятає (вакансій перевіряє до 20 за раз)
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

    def mark_seen(self, wid, links: list, found: int = 0, last: str = "") -> None:
        """Перевірені посилання → у памʼять (і відкинуті теж, щоб завтра не перевіряти знову)."""
        with self.lock:
            data = self._load()
            w = data["watches"].get(str(wid))
            if w is None:
                return
            w["seen"] = (w["seen"] + [u for u in dict.fromkeys(links) if u not in w["seen"]])[-SEEN_KEEP:]
            w["found"] += found
            if last:
                w["last"] = last
            self._save(data)


def task_goal(watch: dict) -> str:
    """Мета для агента на один запуск вартового: з тим, що вже бачено.

    2026-11-08 перші запуски вакансій сказали «нового нема» після кількох пошуків, на які
    пошуковик відповів «No results found», не відкривши жодної сторінки з вакансіями. Тепер:
    сторінки зі списками, якщо вони є в меті, читаються першими, а «нового нема» лише після
    них. Перший запуск (ще нічого не бачено) звітує про все підхоже."""
    if watch["seen"]:
        known = ("Уже бачене (ці посилання не повторюй):\n" + "\n".join(watch["seen"][-40:]) + "\n"
                 "Шукай саме нове з минулої перевірки. Якщо, прочитавши сторінки, нового не знайшла, закінчи "
                 f"finish з текстом рівно «{NOTHING_NEW}».")
    else:
        known = ("Це перша перевірка: перелічи все підхоже, що знайдеш (до 10 найкращих). «Нового нема» тут "
                 "не кажи: якщо справді нічого, поясни, які сторінки прочитала і чому нічого не підійшло.")
    return (f"Регулярна перевірка ({describe(watch['schedule'])}): {watch['goal']}\n"
            "Якщо в меті є посилання на сторінки зі списками, ВІДКРИЙ УСІ ЇХ read_page першими (кожну, не одну): "
            "пошуковик часто нічого не знаходить, а сторінки працюють. Пошук лише як доповнення.\n"
            f"{known}\nКоротко про кожне знайдене, з посиланням.")


def title(goal: str) -> str:
    """Коротка назва вартового для повідомлень: мета без посилань і правил."""
    text = re.split(r"[;\n]|Сторінки зі списками|ПРАВИЛА", URL_RE.sub("", goal or ""))[0]
    return re.sub(r"\s+", " ", text).strip(" ,.:")[:70]


# ── Вартові вакансій: збирає й перевіряє код, модель лише вирішує ─────────────
# 2026-11-08 вартовий-агент то зупинявся після одного списку, то писав, що читав DOU, якого
# не відкривав, то «перевіряв» вакансію з вигаданим номером. Тепер, якщо в меті є сторінки зі
# списками djinni, DOU чи пошуку LinkedIn, посилання на вакансії збирає код, кожну нову сам
# відкриває (лише факти: формат, місто, країни, досвід, зарплата), а модель на кожну каже
# «так» чи «ні». Посилання у звіті з коду, не з моделі, і памʼятаються всі перевірені.

CHECK_PER_RUN = 30          # стільки нових вакансій перевіряє один запуск; решта наступного разу
JUDGE_BATCH = 6             # стільки вакансій в одному питанні моделі (Groq: 8 тисяч токенів на хвилину)
FACTS_LIMIT = 900           # символів фактів однієї вакансії для моделі
PAUSE = {"linkedin.com": 6.0}   # секунд між запитами до сайту: LinkedIn на швидкі поспіль відповідає 429
PAUSE_DEFAULT = 1.0
RETRY_429 = 30              # раз за запуск на сайт: почекати й спробувати ще (LinkedIn відпускає швидко)
STRIKES = 3                 # з ПК: стільки сторінок поспіль сайт не віддав жодній адресі, далі цей запуск без нього
STRIKE_PAUSE = 15           # с: перепочинок сайтові після такої сторінки
SITES = {"djinni.co": "djinni", "jobs.dou.ua": "DOU", "linkedin.com": "LinkedIn", "cvbankas.lt": "cvbankas"}
VERDICT_RE = re.compile(r"^\W*(\d{1,2})\W+(так|ні|yes|no)\b[\s|:;,.)\-]*(.*)$", re.I | re.M)


def _site(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return next((s for s in SITES if host == s or host.endswith("." + s)), "")


def job_link(href: str):
    """Посилання на саму вакансію → її постійна адреса (без хвостів ?position=...), інакше None."""
    site, path = _site(href), urllib.parse.urlsplit(href).path
    if site == "djinni.co" and re.fullmatch(r"/jobs/\d+-[\w-]+/?", path):
        return f"https://djinni.co{path.rstrip('/')}/"
    if site == "jobs.dou.ua" and re.fullmatch(r"/companies/[\w.-]+/vacancies/\d+/?", path):
        return f"https://jobs.dou.ua{path.rstrip('/')}/"
    if site == "cvbankas.lt" and re.fullmatch(r"/[\w-]+/1-\d+/?", path):
        return f"https://en.cvbankas.lt{path.rstrip('/')}"
    m = re.search(r"/jobs/view/(?:[^/]*-)?(\d{6,})/?$", path) if site == "linkedin.com" else None
    return f"https://www.linkedin.com/jobs/view/{m[1]}/" if m else None


def job_sources(goal: str) -> list:
    """Сторінки зі списками вакансій у меті вартового. Пошук LinkedIn з браузера
    (linkedin.com/jobs/search/?keywords=...) стає тим самим пошуком для гостя: його LinkedIn
    віддає без входу (перевірено 2026-11-08 і з сервера, і з дому)."""
    out = []
    for url in (u.rstrip(".,;") for u in URL_RE.findall(goal or "")):
        site, parts = _site(url), urllib.parse.urlsplit(url)
        if site == "linkedin.com":
            if "/jobs-guest/jobs/api/seeMoreJobPostings/search" in parts.path:
                out.append(url)
            elif parts.path.rstrip("/").endswith("/jobs/search"):
                out.append("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + parts.query)
        elif site and job_link(url) is None:
            out.append(url)
    return list(dict.fromkeys(out))


def _facts_url(link: str) -> str:
    """Сторінка вакансії LinkedIn гостеві показує вхід, а цей фрагмент віддає сам опис."""
    m = re.fullmatch(r"https://www\.linkedin\.com/jobs/view/(\d+)/", link)
    return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m[1]}" if m else link


def _refused(error) -> bool:
    """Сайт не пускає саме сервер (обмеження, заборона, мережа), а не «вакансію зняли»."""
    return bool(error) and ("429" in error or "403" in error or "не відкрилась" in error)


class _Pace:
    """Пауза перед повторним запитом до того самого сайту; сторінку, якої сайт не дав серверу,
    пробуємо з домашньої адреси, і навпаки."""

    def __init__(self, sleep):
        self.sleep, self.used, self.retried = sleep, set(), set()
        self.via_home, self.prefer_home, self.home_pages = set(), set(), 0
        self.home_down = False      # ПК вимкнений чи мовчить: цей запуск його вже не питаємо
        self.strikes = {}           # сайт → скільки сторінок поспіль не віддав жодній адресі

    def __call__(self, url: str) -> None:
        site = _site(url)
        if site in self.used:
            self.sleep(PAUSE.get(site, PAUSE_DEFAULT))
        self.used.add(site)

    def fetch(self, get, url: str, home=None):
        """get(url) → (щось, причина або None). Сайт не пускає одну адресу: те саме з другої
        (home: домашній ПК), і далі першою та, що спрацювала. LinkedIn обмежує за частотою з
        адреси й навмання: 2026-11-08 та сама вакансія за хвилину відкрилась з обох, а сусідня
        вдома дала 429, тож одна відмова ще не кінець. Без ПК: один повтор на сайт за запуск
        після RETRY_429 секунд, якщо відповів 429."""
        site = _site(url)
        self(url)
        sources = [("server", get)] + ([("home", home)] if home and not self.home_down else [])
        if site in self.prefer_home:
            sources.reverse()
        got, error = None, None
        for name, fn in sources:
            got_now, error_now = fn(url)
            if name == "home" and error_now and error_now.startswith("ПК "):
                self.home_down = True           # «ПК вимкнений», «ПК не відповів»: це не відповідь сайту
                continue
            got, error = got_now, error_now
            if not _refused(error):             # віддав (або справжнє «нема», 404)
                if name == "home":
                    self.via_home.add(site)
                    self.prefer_home.add(site)
                    self.home_pages += error is None
                else:
                    self.prefer_home.discard(site)
                self.strikes[site] = 0
                return got, error
        if self.home_down or not home:
            if error and "429" in error and site not in self.retried:
                self.retried.add(site)
                self.sleep(RETRY_429)
                got, error = get(url)
                if not _refused(error):
                    return got, error
        else:
            self.sleep(STRIKE_PAUSE)
        self.strikes[site] = self.strikes.get(site, 0) + 1
        return got, error

    def gave_up(self, site: str, home) -> bool:
        """Чи досить на цей запуск: з ПК після STRIKES сторінок поспіль, без ПК після першої."""
        return self.strikes.get(site, 0) >= (STRIKES if home and not self.home_down else 1)


def collect(watch: dict, sleep=time.sleep, home=None) -> dict:
    """Нові вакансії зі списків вартового, кожна з фактами з її власної сторінки.
    → {"jobs": [(посилання, факти)], "checked": що запамʼятати, "later": скільки нових на потім,
       "sources": [(список, скільки вакансій або причина)], "blocked": сайти, що обмежили, "steps": журнал,
       "via_home": сайти, читані через домашній ПК}
    home(вид, адреса) → (посилання чи текст, None) або (порожнє, причина): сторінка з домашнього ПК."""
    seen, pace = set(watch.get("seen") or []), _Pace(sleep)
    home_links = (lambda url: home("links", url)) if home else None
    home_text = (lambda url: home("text", url)) if home else None
    groups, sources, steps = [], [], []
    for src in job_sources(watch["goal"]):
        hrefs, error = pace.fetch(tools.page_links, src, home_links)
        links = list(dict.fromkeys(l for l in map(job_link, hrefs) if l))
        new = [l for l in links if l not in seen]
        sources.append((src, error or len(links)))
        steps.append(f"список: {src[:90]} ({error or f'нових {len(new)} з {len(links)}'})")
        groups.append(new)
    queue = []                                  # по черзі з кожного списку, щоб жоден не витіснив інші
    for row in itertools.zip_longest(*groups):
        for link in row:                        # та сама вакансія буває у двох пошуках LinkedIn
            if link and link not in queue:
                queue.append(link)
    jobs, checked, blocked, later = [], [], set(), 0
    for link in queue:
        site = _site(link)
        if len(jobs) >= CHECK_PER_RUN or site in blocked:
            later += 1
            continue
        text, error = pace.fetch(lambda url: tools.page_text(url, limit=20000), _facts_url(link), home_text)
        if _refused(error):                     # не запамʼятовуємо: спробуємо наступного разу
            later += 1
            steps.append(f"не відкрилась: {link} ({error})")
            if pace.gave_up(site, home):
                blocked.add(site)
            continue
        checked.append(link)
        if error:                               # 404 і подібне: вакансію зняли
            steps.append(f"не відкрилась: {link} ({error})")
            continue
        steps.append(f"перевіряю: {link}")
        head = 3 if site == "linkedin.com" else 1
        jobs.append((link, tools.facts_of(text, head=head)[:FACTS_LIMIT]))
    via_home = sorted(SITES[s] for s in pace.via_home)
    if via_home:
        steps.append(f"через домашній ПК ({', '.join(via_home)}): {pace.home_pages} сторінок")
    return {"jobs": jobs, "checked": checked, "later": later, "sources": sources,
            "blocked": sorted(SITES[s] for s in blocked), "steps": steps, "via_home": via_home}


def judge_prompt(goal: str, batch: list) -> str:
    criteria = re.sub(r"\s+", " ", URL_RE.sub("", goal)).strip()
    jobs = "\n\n".join(f"[{i}]\n{facts}" for i, (_, facts) in enumerate(batch, 1))
    return ("Ти Лін і допомагаєш Владу шукати роботу. Його критерії:\n"
            f"{criteria}\n\n"
            "Нижче нові вакансії: назва й рядки з фактами з їхніх власних сторінок. Для КОЖНОЇ рівно один рядок.\n"
            "Підходить: номер | так | посада | компанія | напрям (одне з: Python, AI/LLM, QA, Data, Web) | місто й "
            "формат | зарплата як на сторінці | відповідність 0-100 | одним реченням чому (і чи вимагають литовську)\n"
            "Не підходить: номер | ні | посада, компанія: причина\n"
            "Пиши українською. Чого у фактах нема, лиши поле порожнім, без прочерків. Вирішуй лише за цими фактами, "
            "нічого не додавай від себе. Якщо фактів не вистачає, щоб відкинути, пиши «так» і в кінці «(перевір)». "
            "Рівень «Mid-Senior level» у LinkedIn стоїть у більшості вакансій, і в middle теж: сам по собі це не "
            "senior; senior лише тоді, коли Senior чи Lead у назві посади або досвід від 5 років. "
            "Без розмітки й без нічого, крім цих рядків.\n\n" + jobs)


def _clean(why: str) -> str:
    """Рядок моделі для звіту: «(перевір)» у кінець, без порожніх полів-прочерків і довгих тире."""
    check = "(перевір)" in why
    why = URL_RE.sub("", why)                                   # посилання у звіт ставить код, не модель
    why = re.sub(r"[\u2014\u2013]", "-", why.replace("(перевір)", ""))
    why = re.sub(r"\s+-\s*(?=,)", "", why)                       # «Twoday - , офіс» → «Twoday, офіс»
    why = re.sub(r"(?<=[,:])\s*-\s*(?=[,;]|$)", "", why)       # «Senior, прочерк, прочерк; ...» → «Senior,,; ...»
    why = re.sub(r",\s*(?=[,;]|$)", "", why)
    why = re.sub(r":\s*,", ":", why)
    why = re.sub(r"^[\s|:;,.\-]+|[\s|:;,\-]+$", "", why)
    return why + (" (перевір)" if check else "")


def _fit_fields(rest: str, facts: str) -> dict:
    """Рядок «так» → поля для звіту й трекера. Модель не дотрималась формату: увесь рядок
    іде в звіт як є, а назва для трекера з першого рядка сторінки."""
    parts = [p.strip() for p in rest.split("|")]
    if len(parts) >= 7:
        title, company, area, place, salary, match = (_clean(p) for p in parts[:6])
        why = _clean(" | ".join(parts[6:]))
        head = ", ".join(x for x in (title, company) if x)
        tail = ", ".join(x for x in (place, salary) if x)
        line = head + (f": {tail}" if tail else "") + (f". {why}" if why else "")
    else:
        title, company, area, place, salary, match, why = _name(facts), "", "", "", "", "", _clean(rest)
        line = why or title
    m = re.search(r"\d{1,3}", match)
    return {"title": title[:120], "company": company[:80], "area": area[:30], "place": place[:80],
            "salary": salary[:60], "match": min(int(m[0]), 100) if m else "", "why": why[:300], "line": line}


def _name(facts: str) -> str:
    """Назва вакансії з першого рядка її сторінки, без хвоста з назвою сайту (DOU, Djinni)."""
    first = (facts or "").strip().splitlines()[0] if (facts or "").strip() else ""
    first = re.sub(r"\s*[|\u2013\u2014-]+\s*(Djinni|DOU|LinkedIn)\b.*$", "", first, flags=re.I)
    return re.sub(r"[\u2014\u2013]", "-", first).strip()[:70]


def check_jobs(watch: dict, judge, sleep=time.sleep, home=None) -> dict:
    """Один запуск вартового вакансій. judge(питання) → відповідь моделі текстом; home: див. collect.
    → {"report": текст для Влада або "" (мовчати), "seen": перевірені посилання, "fit": скільки
       підійшло, "fits": поля підхожих для трекера (з "url"), "summary": рядок для /watches, "steps": журнал}"""
    found = collect(watch, sleep, home)
    jobs, verdicts = found["jobs"], {}
    for start in range(0, len(jobs), JUDGE_BATCH):
        batch = jobs[start:start + JUDGE_BATCH]
        for n, yes, rest in VERDICT_RE.findall(judge(judge_prompt(watch["goal"], batch)) or ""):
            if 1 <= int(n) <= len(batch):
                link, facts = batch[int(n) - 1]
                ok = yes.lower() in ("так", "yes")
                verdicts[link] = (ok, _fit_fields(rest, facts) if ok else _clean(rest))
    fit = [(l, verdicts[l][1]) for l, _ in jobs if l in verdicts and verdicts[l][0]]
    rejected = []
    for link, facts in jobs:                    # назва з коду, якщо модель її в рядку не написала
        if link in verdicts and not verdicts[link][0]:
            why, name = verdicts[link][1], _name(facts)
            rejected.append(why if not name or name[:12].lower() in why.lower() else f"{name}: {why}")
    unclear = [l for l, _ in jobs if l not in verdicts]
    first = not watch.get("seen")
    broken = bool(found["sources"]) and all(isinstance(n, str) for _, n in found["sources"])

    lines = []
    if broken:
        lines.append("Жодна сторінка зі списком не відкрилась: " +
                     "; ".join(f"{SITES[_site(s)]}: {n}" for s, n in found["sources"]))
    if fit:
        lines.append(f"Підходять {len(fit)} з {len(jobs)} нових:")
        lines += [f"{i}. {f['line']}\n  {link}" for i, (link, f) in enumerate(fit, 1)]
    if unclear:
        lines.append("Модель про ці не відповіла, глянь сам:")
        lines += [f"- {link}" for link in unclear]
    if not jobs and not broken:
        lines.append("Нових вакансій у списках нема.")
    if rejected and (fit or unclear or first):
        lines.append(f"Відкинула {len(rejected)}:")
        lines += [f"- {why}" for why in rejected[:8]] + (["- ..."] if len(rejected) > 8 else [])
    counts, errors = {}, []
    for src, n in found["sources"]:
        name = SITES[_site(src)]
        counts[name] = counts.get(name, 0) + (n if isinstance(n, int) else 0)
        if isinstance(n, str) and not broken:
            errors.append(f"{name}: {n.rstrip('.')}")
    footer = "Списки: " + ", ".join(f"{k} {v}" for k, v in counts.items())
    if errors:
        footer += "; не відкрились " + "; ".join(dict.fromkeys(errors))
    if found["later"]:
        footer += f"; ще {found['later']} нових перевірю наступного разу"
    if found["blocked"]:
        footer += f" ({', '.join(found['blocked'])} обмежив запити)"
    if found["via_home"]:
        footer += f"; {', '.join(found['via_home'])} читала через домашній ПК"
    footer += "."
    loud = bool(fit or unclear or first or broken)
    summary = f"{tools.now():%d.%m %H:%M} нових {len(jobs)}, підійшло {len(fit)}"
    return {"report": "\n".join(lines + ["", footer]).strip() if loud else "", "seen": found["checked"],
            "fit": len(fit), "fits": [dict(f, url=link) for link, f in fit], "summary": summary,
            "steps": found["steps"]}


def listing(watches: list) -> str:
    if not watches:
        return ("Вартових нема. Постав: /watch щодня 09:00 нові вакансії Python у Вільнюсі "
                "(або «щоранку», «кожні 6 год»).")
    out = ["Вартові:"]
    for w in watches:
        when = datetime.fromisoformat(w["next"])
        last = f", остання {w['last']}" if w.get("last") else ""
        out.append(f"#{w['id']} {describe(w['schedule'])}: {title(w['goal'])} "
                   f"(далі {when:%d.%m %H:%M}, знайдено {w['found']}{last})")
    return "\n".join(out)
