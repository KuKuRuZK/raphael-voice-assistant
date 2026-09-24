"""
Gmail: кілька скриньок, сортування mail_triage, читання, пошук, чернетки
і перевірка нових листів для монітора. Тут же спільні OAuth-креденшели Google.
"""
import functools
import itertools
import logging
import os
import threading
from datetime import datetime

from raphael import llm
from raphael import mail_triage
from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_seen_gmail_ids: dict = {}   # вже оголошені листи; dict, а не set, бо треба порядок вставки

# ============================================================
#  GMAIL
# ============================================================
_google_creds_cache = None

# httplib2 під googleapiclient не потокобезпечний, а поштою одночасно
# користуються монітор, дайджест і голосові команди. Два потоки на одному
# зʼєднанні зрідка давали SSL-помилки, тож запити до Google йдуть по одному.
_api_lock = threading.RLock()


def _one_at_a_time(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _api_lock:
            return fn(*args, **kwargs)
    return wrapper


# ── Відвалена авторизація ────────────────────────────────────────────────────
# Поки OAuth-застосунок Google у режимі Testing, refresh-токен живе 7 днів, і
# потім пошта з календарем мовчки переставали працювати: помилка була лише в
# лозі. Тепер скринька з мертвим токеном потрапляє в _auth_dead. Монітор раз на
# день каже про це голосом, а на пряме питання («що по пошті») Рафаель
# відповідає справжньою причиною замість «скринька чиста». Закешований сервіс
# при цьому скидається, тож після gmail_auth.bat новий токен підхопиться з
# диска на наступній перевірці, без перезапуску.
_auth_dead: dict = {}   # token_path → причина
_auth_warned_on = ""    # дата, коли про це вже сказали голосом


def _is_auth_error(e) -> bool:
    s = str(e).lower()
    return (type(e).__name__ == "RefreshError" or "invalid_grant" in s
            or "unauthorized_client" in s or "token has been expired or revoked" in s)


def _auth_problem(token_path: str, reason) -> None:
    """Токен більше не дійсний: забути закешовані сервіси й запамʼятати скриньку."""
    global _google_creds_cache
    _gmail_multi_cache.pop(token_path, None)
    if token_path == cfg.GMAIL_TOKEN_PATH:
        _google_creds_cache = None
    if token_path not in _auth_dead:      # у лог один раз, а не на кожній перевірці
        log.error(f"Google: доступ за {os.path.basename(token_path)} більше не дійсний: {reason}")
    _auth_dead[token_path] = str(reason)


def _auth_ok(token_path: str) -> None:
    if _auth_dead.pop(token_path, None) is not None:
        log.info(f"Google: доступ за {os.path.basename(token_path)} відновлено")


def auth_warning(daily: bool = False) -> str:
    """
    Що сказати про скриньки без доступу ("" якщо таких немає).
    daily=True для монітора: не частіше разу на день і не того дня, коли
    про це вже сказали у відповідь на питання.
    """
    global _auth_warned_on
    today = datetime.now().strftime("%Y-%m-%d")
    if not _auth_dead or (daily and _auth_warned_on == today):
        return ""
    _auth_warned_on = today
    tokens = [a["token"] for a in cfg.GMAIL_ACCOUNTS]
    out = []
    for token_path in list(_auth_dead):   # календар дописує з іншого потоку
        if token_path in tokens:
            idx = tokens.index(token_path)
            label = cfg.GMAIL_ACCOUNTS[idx]["label"]
        else:
            idx, label = 0, "основна"
        what = f"пошти «{label}»" + (" і календаря" if token_path == cfg.GMAIL_TOKEN_PATH else "")
        arg = f" {idx + 1}" if idx else ""
        out.append(f"Немає доступу до {what}: треба знову увійти в Google. Запусти gmail_auth.bat{arg}.")
    return " ".join(out)


def _get_google_creds():
    """
    Спільні OAuth2 креденшели для Gmail і Calendar (один токен, з кешу).

    Ніколи не відкриває вхід у браузері: раніше без дійсного токена фоновий
    потік сам запускав run_local_server і чекав, поки хтось увійде. Тепер
    Рафаель просить запустити gmail_auth.bat.
    """
    global _google_creds_cache
    if _google_creds_cache is not None:
        return _google_creds_cache
    if not os.path.exists(cfg.GMAIL_CREDENTIALS_PATH):
        return None
    with _api_lock:   # оновлення токена пише файл, і робити це має один потік
        if _google_creds_cache is not None:
            return _google_creds_cache
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            creds = None
            if os.path.exists(cfg.GMAIL_TOKEN_PATH):
                creds = Credentials.from_authorized_user_file(cfg.GMAIL_TOKEN_PATH, cfg.GMAIL_SCOPES)

            if not creds or not creds.valid:
                if not (creds and creds.expired and creds.refresh_token):
                    _auth_problem(cfg.GMAIL_TOKEN_PATH, "немає дійсного токена")
                    return None
                creds.refresh(Request())
                with open(cfg.GMAIL_TOKEN_PATH, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())

            _google_creds_cache = creds
            _auth_ok(cfg.GMAIL_TOKEN_PATH)
            return creds
        except Exception as e:
            if _is_auth_error(e):
                _auth_problem(cfg.GMAIL_TOKEN_PATH, e)
            else:
                log.error(f"Google creds init: {e}")
            return None


# ── Мульти-акаунт пошта ───────────────────────────────────────────────────────
_gmail_multi_cache: dict = {}   # token_path → service


@_one_at_a_time
def _get_gmail_service(token_path: str):
    """Будує Gmail сервіс для конкретного токена (без інтерактивного входу)."""
    if token_path in _gmail_multi_cache:
        return _gmail_multi_cache[token_path]
    if not os.path.exists(cfg.GMAIL_CREDENTIALS_PATH) or not os.path.exists(token_path):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(token_path, cfg.GMAIL_SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        svc = build("gmail", "v1", credentials=creds)
        _gmail_multi_cache[token_path] = svc
        _auth_ok(token_path)
        return svc
    except Exception as e:
        if _is_auth_error(e):
            _auth_problem(token_path, e)
        else:
            log.error(f"Gmail service {os.path.basename(token_path)}: {e}")
        return None


def _gmail_accounts() -> list:
    """Список (label, service) для всіх авторизованих скриньок."""
    out = []
    for acc in cfg.GMAIL_ACCOUNTS:
        svc = _get_gmail_service(acc["token"])
        if svc:
            out.append((acc["label"], svc))
    return out


def _gmail_box(label: str) -> str:
    """Який набір правил сортування застосовувати до цієї скриньки."""
    for acc in cfg.GMAIL_ACCOUNTS:
        if acc["label"] == label:
            return acc.get("box", "основна")
    return "основна"


def _account_error(label: str, what: str, e) -> None:
    """Помилка однієї скриньки: мертвий токен відмічаємо, решту пишемо в лог."""
    if _is_auth_error(e):
        _auth_problem(next((a["token"] for a in cfg.GMAIL_ACCOUNTS if a["label"] == label),
                           cfg.GMAIL_TOKEN_PATH), e)
    else:
        log.error(f"Gmail {what} [{label}]: {e}")


def _mail_tail(failed: list, multi: bool) -> str:
    """Що додати до відповіді, якщо якісь скриньки не вдалося перевірити."""
    warning = auth_warning()
    if warning or not failed:
        return warning
    what = "скриньку " + ", ".join(f"«{label}»" for label in failed) if multi else "пошту"
    return f"Не вдалося перевірити {what}."


_triage_label_cache: dict = {}   # token_path -> {категорія: labelId}


def _triage_labels(svc, token_path: str) -> dict:
    """{категорія: labelId} для скриньки; мітки створюються при першому зверненні."""
    labels = _triage_label_cache.get(token_path)
    if labels is None:
        labels = mail_triage.ensure_labels(svc)
        _triage_label_cache[token_path] = labels
    return labels


def _gmail_triage(svc, token_path: str, msg_id: str, sender: str, subject: str, box: str):
    """
    Вішає мітку Трекер/* на один лист і прибирає шум із вхідних.
    Повертає категорію, або None якщо сортування вимкнене чи щось пішло не так.

    Помилка тут не має ламати монітор: не змогли повісити мітку, то й добре,
    лист все одно буде оголошений, якщо він того вартий.
    """
    if not (cfg.MAIL_TRIAGE_ENABLED and mail_triage):
        return None
    try:
        cat = mail_triage.classify(sender, subject, box)
        labels = _triage_labels(svc, token_path)
        body = {"addLabelIds": [labels[cat]]}
        if cat in mail_triage.ARCHIVE:
            body["removeLabelIds"] = ["INBOX"]
        svc.users().messages().modify(userId="me", id=msg_id, body=body).execute()
        return cat
    except Exception as e:
        log.error(f"Сортування листа {msg_id}: {e}")
        return None


@_one_at_a_time
def _gmail_find(query: str):
    """Знаходить перший лист за запитом серед усіх скриньок. → (label, svc, msg_id) або None."""
    q = query.strip() or "is:unread"
    for label, svc in _gmail_accounts():
        try:
            res = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
            msgs = res.get("messages", [])
            if msgs:
                return label, svc, msgs[0]["id"]
        except Exception as e:
            _account_error(label, "find", e)
    return None


def _gmail_parse_sender(raw: str) -> str:
    """'John Doe <john@example.com>' → 'John Doe'"""
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"').strip("'")
    return raw.strip()


def _gmail_headers(msg: dict) -> dict:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


@_one_at_a_time
def _gmail_unread_text(limit: int = 5) -> str:
    """Непрочитані листи з усіх скриньок текстом: скільки і від кого."""
    accounts = _gmail_accounts()
    if not accounts:
        return auth_warning() or "Gmail не налаштований. Потрібен файл gmail_credentials.json в папці асистента."
    multi = len(accounts) > 1
    parts = []
    failed = []
    grand_total = 0
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(
                userId="me", labelIds=["UNREAD", "INBOX"], maxResults=limit
            ).execute()
            msgs  = res.get("messages", [])
            total = res.get("resultSizeEstimate", len(msgs))
            grand_total += total
            if not msgs:
                parts.append(f"{label}: чисто" if multi else "")
                continue
            senders = []
            for m in msgs[:3]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata", metadataHeaders=["From"]
                ).execute()
                senders.append(_gmail_parse_sender(_gmail_headers(full).get("From", "?")))
            uniq = ", ".join(dict.fromkeys(senders))
            if multi:
                parts.append(f"{label}: {total} від {uniq}")
            else:
                parts.append(f"У тебе {vr.count(total, 'непрочитаний лист', 'непрочитані листи', 'непрочитаних листів')}, від {uniq}")
            log.info(f"Gmail unread [{label}]: {total}")
        except Exception as e:
            _account_error(label, "unread", e)
            failed.append(label)

    # «Скринька чиста» лише тоді, коли її справді перевірили: раніше без
    # мережі чи з мертвим токеном Рафаель бадьоро казав, що листів немає
    parts = [p for p in parts if p]
    if grand_total:
        text = ("Пошта. " if multi else "") + ". ".join(parts) + "."
    elif len(failed) < len(accounts):
        text = "Непрочитаних листів немає."
        if not failed and not _auth_dead:
            text += " Усі скриньки чисті." if multi else " Поштова скринька чиста."
    else:
        text = ""
    return f"{text} {_mail_tail(failed, multi)}".strip()


def _gmail_unread(limit: int = 5) -> None:
    """Читає непрочитані листи з усіх скриньок — скільки і від кого."""
    tts.speak(_gmail_unread_text(limit))


@_one_at_a_time
def _gmail_latest(limit: int = 3) -> None:
    """Читає останні листи з усіх скриньок."""
    accounts = _gmail_accounts()
    if not accounts:
        tts.speak(auth_warning() or "Gmail не налаштований.")
        return
    multi = len(accounts) > 1
    parts = []
    failed = []
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], maxResults=limit
            ).execute()
            msgs = res.get("messages", [])
            if not msgs:
                continue
            lines = []
            for m in msgs[:limit]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject"]
                ).execute()
                h = _gmail_headers(full)
                sender  = _gmail_parse_sender(h.get("From", "невідомо"))
                subject = h.get("Subject", "без теми")
                lines.append(f"{sender}: «{subject}»")
            prefix = f"{label} — " if multi else ""
            parts.append(prefix + "; ".join(lines))
        except Exception as e:
            _account_error(label, "latest", e)
            failed.append(label)
    tail = _mail_tail(failed, multi)
    if not parts:
        tts.speak(tail or "Листів немає.")
        return
    tts.speak(f"Останні листи: {'. '.join(parts)}. {tail}".strip())


@_one_at_a_time
def _gmail_search(query: str, limit: int = 3) -> None:
    """Шукає листи за запитом серед усіх скриньок (синтаксис Gmail: from:, subject:)."""
    accounts = _gmail_accounts()
    if not accounts:
        tts.speak(auth_warning() or "Gmail не налаштований.")
        return
    multi = len(accounts) > 1
    parts = []
    failed = []
    found = 0
    for label, svc in accounts:
        try:
            res = svc.users().messages().list(userId="me", q=query, maxResults=limit).execute()
            msgs = res.get("messages", [])
            if not msgs:
                continue
            lines = []
            for m in msgs[:limit]:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject"]
                ).execute()
                h = _gmail_headers(full)
                sender  = _gmail_parse_sender(h.get("From", "невідомо"))
                subject = h.get("Subject", "без теми")
                lines.append(f"{sender}: «{subject}»")
            found += len(lines)
            prefix = f"{label} — " if multi else ""
            parts.append(prefix + "; ".join(lines))
        except Exception as e:
            _account_error(label, "search", e)
            failed.append(label)
    tail = _mail_tail(failed, multi)
    if not found:
        tts.speak(f"По запиту «{query}» листів не знайдено. {tail}".strip())
        return
    tts.speak(f"Знайшла {vr.count(found, 'лист', 'листи', 'листів')} по «{query}»: "
              f"{'. '.join(parts)}. {tail}".strip())


# ============================================================
#  GMAIL — ПОВНЕ ЧИТАННЯ + ЧЕРНЕТКИ
# ============================================================

def _gmail_extract_body(msg: dict) -> str:
    """Витягує текстове тіло листа з payload (обходить MIME-частини)."""
    import base64

    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    payload = msg.get("payload", {})

    def _walk(part) -> str:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return _decode(body["data"])
        if mime.startswith("multipart") or part.get("parts"):
            for sub in part.get("parts", []):
                txt = _walk(sub)
                if txt:
                    return txt
        if mime == "text/html" and body.get("data"):
            import re as _re
            return _re.sub(r"<[^>]+>", " ", _decode(body["data"]))
        return ""

    text = _walk(payload)
    if not text and payload.get("body", {}).get("data"):
        text = _decode(payload["body"]["data"])
    return text.strip()


def _gmail_read_full(query: str = "") -> None:
    """Читає вголос повний текст листа (останнього непрочитаного або за запитом, з усіх скриньок)."""
    if not _gmail_accounts():
        tts.speak(auth_warning() or "Gmail не налаштований.")
        return
    found = _gmail_find(query)
    if not found:
        tts.speak(f"Такого листа не знайшла. {auth_warning()}".strip())
        return
    label, svc, msg_id = found
    try:
        with _api_lock:   # не на весь лист: читання вголос триває хвилину
            full = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        h = _gmail_headers(full)
        sender  = _gmail_parse_sender(h.get("From", "невідомо"))
        subject = h.get("Subject", "без теми")
        body = _gmail_extract_body(full)
        body = " ".join(body.split())   # прибираємо зайві пробіли/переноси

        if len(body) > 600:
            # Довгий лист стискає модель. Заголовок звучить одразу, переказ
            # потоком слідом; не вдалося переказати, читаємо початок листа.
            def _summary():
                stream = llm.llm_stream(
                    cfg.GROQ_PRIMARY_MODEL,
                    messages=[{"role": "user", "content":
                        f"Перекажи КОРОТКО українською (2-3 речення) суть цього листа:\n\n{body[:2500]}"}],
                    max_tokens=180, temperature=0.3,
                )
                try:
                    first = next(stream, "")
                except Exception as e:
                    log.error(f"Gmail переказ: {e}")
                    yield f"Переказати не вийшло, ось початок: {body[:500]}…"
                    return
                yield "Коротко: " + first
                yield from stream

            tts.speak_stream(itertools.chain([f"Лист від {sender}, тема «{subject}». "], _summary()))
            log.info(f"Gmail read full: {subject}")
            return
        tts.speak(f"Лист від {sender}, тема «{subject}». {body}")
        log.info(f"Gmail read full: {subject}")
    except Exception as e:
        log.error(f"Gmail read full: {e}")
        tts.speak("Не вдалося прочитати лист.")


@_one_at_a_time
def _gmail_draft_reply(query: str, reply_text: str) -> None:
    """Створює ЧЕРНЕТКУ відповіді на лист (не відправляє — для безпеки)."""
    if not _gmail_accounts():
        tts.speak(auth_warning() or "Gmail не налаштований.")
        return
    if not reply_text.strip():
        tts.speak("Що саме відповісти?")
        return
    found = _gmail_find(query)
    if not found:
        tts.speak(f"Не знайшла на що відповідати. {auth_warning()}".strip())
        return
    label, svc, msg_id = found
    try:
        import base64
        from email.message import EmailMessage

        orig = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"]
        ).execute()
        h = _gmail_headers(orig)
        # Відповідаємо туди, куди просить відправник (Reply-To), інакше на From
        to_addr = h.get("Reply-To") or h.get("From", "")
        subject = h.get("Subject", "")
        msg_id  = h.get("Message-ID", "")
        thread_id = orig.get("threadId")

        # EmailMessage, а не MIMEText: старий API кодував «Іван <ivan@x.com>»
        # цілком в один шматок, і адреса отримувача губилась, щойно імʼя
        # було кирилицею чи з литовськими літерами.
        mime = EmailMessage()
        mime["To"] = to_addr
        mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if msg_id:
            mime["In-Reply-To"] = msg_id
            mime["References"]  = f"{h.get('References', '')} {msg_id}".strip()
        mime.set_content(reply_text)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": thread_id}}
        ).execute()
        who = _gmail_parse_sender(to_addr)
        tts.speak(f"Створила чернетку відповіді для {who}. Перевір у Gmail перед відправкою.")
        log.info(f"Gmail draft reply to {who}")
    except Exception as e:
        log.error(f"Gmail draft reply: {e}")
        tts.speak("Не вдалося створити чернетку.")


# ============================================================
#  МОНІТОРИНГ — ПРОАКТИВНІ СПОВІЩЕННЯ
# ============================================================

@_one_at_a_time
def _gmail_check_new() -> list:
    """Повертає НОВІ непрочитані листи (label, sender, subject) з усіх скриньок."""
    accounts = _gmail_accounts()
    if not accounts:
        return []
    multi = len(accounts) > 1
    new_items = []
    listed = set()        # ключі листів, які зараз лежать у непрочитаних вхідних
    ok_accounts = set()   # скриньки, які цього разу вдалося перевірити
    try:
        for label, svc in accounts:
            try:
                box = _gmail_box(label)
                token_path = next((a["token"] for a in cfg.GMAIL_ACCOUNTS
                                   if a["label"] == label), label)
                # Мітки Трекер/* і є памʼяттю монітора: лист із такою міткою вже
                # розкладено (у минулому проході чи до перезапуску) і, якщо він
                # того вартий, уже оголошено. Памʼять у RAM цього не переживала.
                processed = set()
                if cfg.MAIL_TRIAGE_ENABLED and mail_triage:
                    try:
                        processed = set(_triage_labels(svc, token_path).values())
                    except Exception as e:
                        log.error(f"Мітки Трекер [{label}]: {e}")
                # 40, а не 10: сортувальник має встигати за напливом розсилок,
                # інакше вхідні знову заростуть між перевірками
                res = svc.users().messages().list(
                    userId="me", labelIds=["UNREAD", "INBOX"], maxResults=40
                ).execute()
                msgs = res.get("messages", [])
                for m in msgs:
                    key = f"{label}:{m['id']}"   # унікальний ключ на акаунт
                    listed.add(key)
                    if key in _seen_gmail_ids:
                        continue
                    _seen_gmail_ids[key] = True
                    full = svc.users().messages().get(
                        userId="me", id=m["id"], format="metadata",
                        metadataHeaders=["From", "Subject"]
                    ).execute()
                    if processed & set(full.get("labelIds", [])):
                        continue                  # уже розкладений раніше
                    h = _gmail_headers(full)
                    raw_from = h.get("From", "невідомо")
                    subject  = h.get("Subject", "без теми")

                    # Розкласти по мітках і прибрати шум із вхідних
                    cat = _gmail_triage(svc, token_path, m["id"], raw_from, subject, box)

                    # Голосом озвучуємо лише те, що того варте. Якщо сортування
                    # не спрацювало (cat is None), поводимось як раніше й кажемо
                    # про все: краще зайвий раз сказати, ніж мовчки проковтнути.
                    if cat is not None and cat not in mail_triage.ANNOUNCE:
                        continue

                    sender = _gmail_parse_sender(raw_from)
                    if multi:
                        sender = f"{sender} ({label})"
                    if cat:
                        sender = f"{sender}, {cat}"
                    new_items.append((m["id"], sender, subject))
                ok_accounts.add(label)
            except Exception as e:
                _account_error(label, "monitor", e)
        # Забуваємо лише листи, яких уже немає в непрочитаних вхідних (прочитані,
        # заархівовані). Раніше при переповненні викидались НАЙСТАРІШІ ключі,
        # тобто якраз важливі непрочитані, і їх оголошувало вдруге як нові.
        for key in list(_seen_gmail_ids):
            if key.rsplit(":", 1)[0] in ok_accounts and key not in listed:
                del _seen_gmail_ids[key]
        return new_items
    except Exception as e:
        log.error(f"Gmail monitor check: {e}")
        return []
