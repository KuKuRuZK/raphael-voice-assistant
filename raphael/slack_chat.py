"""
Slack: непрочитані, згадки, приватні повідомлення, канали.
Не slack.py: така назва зіткнулась би з пакетом slack зі slack_sdk.
"""
import logging

from raphael import settings as cfg
from raphael import tts
from raphael import voice_rules as vr

log = logging.getLogger("Лін")


_seen_slack_ts:  float = 0.0   # час останнього оголошеного повідомлення Slack

# ============================================================
#  SLACK
# ============================================================
_slack_client_cache = None


def _get_slack():
    """Повертає ініціалізований Slack WebClient (з кешу)."""
    global _slack_client_cache
    if _slack_client_cache is not None:
        return _slack_client_cache
    if not cfg.SLACK_TOKEN:
        return None
    try:
        from slack_sdk import WebClient
        cl = WebClient(token=cfg.SLACK_TOKEN)
        cl.auth_test()   # перевірка — кидає помилку якщо токен невалідний
        _slack_client_cache = cl
        log.info("Slack клієнт ініціалізовано")
        return _slack_client_cache
    except Exception as e:
        log.error(f"Slack init: {e}")
        return None


def _slack_user_name(sl, user_id: str) -> str:
    """Повертає читабельне ім'я Slack-користувача."""
    try:
        info = sl.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("real_name") or profile.get("display_name") or user_id
    except Exception:
        return user_id


def _slack_unread(limit: int = 5) -> None:
    """Читає непрочитані повідомлення через search API."""
    sl = _get_slack()
    if not sl:
        tts.speak("Slack не налаштований. Додай SLACK_TOKEN в конфіг lin.py.")
        return
    try:
        result = sl.search_messages(query="is:unread", count=limit, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            tts.speak("Непрочитаних повідомлень у Slack немає.")
            return
        lines = []
        for m in matches[:limit]:
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            ch    = m.get("channel", {}).get("name", "")
            ch_str = f"в #{ch}" if ch else ""
            lines.append(f"{uname} {ch_str}: {text}")
        tts.speak(f"Непрочитані в Slack ({len(matches)}): " + "; ".join(lines) + ".")
        log.info(f"Slack unread: {len(matches)} повідомлень")
    except Exception as e:
        log.error(f"Slack unread: {e}")
        tts.speak("Не вдалося прочитати Slack. Перевір токен і scope search:read.")


def _slack_channel(channel_name: str, limit: int = 5) -> None:
    """Читає останні повідомлення з каналу."""
    sl = _get_slack()
    if not sl:
        tts.speak("Slack не налаштований.")
        return
    try:
        # Шукаємо канал серед доступних
        resp = sl.conversations_list(types="public_channel,private_channel", limit=200)
        channels = resp.get("channels", [])
        ch = next(
            (c for c in channels if channel_name.lower() in c["name"].lower()),
            None
        )
        if not ch:
            tts.speak(f"Канал «{channel_name}» не знайдено.")
            return
        hist = sl.conversations_history(channel=ch["id"], limit=limit)
        msgs = hist.get("messages", [])
        if not msgs:
            tts.speak(f"В #{ch['name']} порожньо.")
            return
        lines = []
        for m in reversed(msgs[:limit]):
            uid   = m.get("user", "")
            uname = _slack_user_name(sl, uid) if uid else "бот"
            text  = (m.get("text") or "")[:100]
            lines.append(f"{uname}: {text}")
        tts.speak(f"#{ch['name']}: " + "; ".join(lines) + ".")
        log.info(f"Slack channel #{ch['name']}: {len(lines)} повідомлень")
    except Exception as e:
        log.error(f"Slack channel: {e}")
        tts.speak("Не вдалося прочитати канал.")


def _slack_dm(limit: int = 5) -> None:
    """Читає останні приватні повідомлення (DM)."""
    sl = _get_slack()
    if not sl:
        tts.speak("Slack не налаштований.")
        return
    try:
        ims = sl.conversations_list(types="im", limit=20).get("channels", [])
        if not ims:
            tts.speak("Немає активних DM у Slack.")
            return
        all_msgs = []
        for im in ims[:8]:
            hist = sl.conversations_history(channel=im["id"], limit=2).get("messages", [])
            for m in hist:
                text = (m.get("text") or "").strip()
                if not text:
                    continue
                uid   = im.get("user", "")
                uname = _slack_user_name(sl, uid) if uid else "хтось"
                ts    = float(m.get("ts", 0))
                all_msgs.append((ts, uname, text[:80]))
        if not all_msgs:
            tts.speak("Нових DM немає.")
            return
        all_msgs.sort(reverse=True)
        lines = [f"{u}: {t}" for _, u, t in all_msgs[:limit]]
        tts.speak("Приватні повідомлення: " + "; ".join(lines) + ".")
        log.info(f"Slack DM: {len(lines)} повідомлень")
    except Exception as e:
        log.error(f"Slack DM: {e}")
        tts.speak("Не вдалося прочитати DM.")


def _slack_mentions(limit: int = 5) -> None:
    """Читає згадки (@you) через search."""
    sl = _get_slack()
    if not sl:
        tts.speak("Slack не налаштований.")
        return
    try:
        result = sl.search_messages(query="is:mention", count=limit, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            tts.speak("Згадок у Slack немає.")
            return
        lines = []
        for m in matches[:limit]:
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            lines.append(f"{uname}: {text}")
        tts.speak(f"Тебе згадали {vr.count(len(matches), 'раз', 'рази', 'разів')}: " + "; ".join(lines) + ".")
        log.info(f"Slack mentions: {len(matches)}")
    except Exception as e:
        log.error(f"Slack mentions: {e}")
        tts.speak("Не вдалося отримати згадки.")


def _slack_check_new() -> list:
    """Повертає НОВІ згадки/DM (username, text) з останньої перевірки."""
    global _seen_slack_ts
    sl = _get_slack()
    if not sl:
        return []
    try:
        result = sl.search_messages(query="is:mention", count=10, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        new_items = []
        max_ts = _seen_slack_ts
        for m in matches:
            ts = float(m.get("ts", 0))
            if ts <= _seen_slack_ts:
                continue
            max_ts = max(max_ts, ts)
            uname = m.get("username") or "хтось"
            text  = (m.get("text") or "")[:80]
            new_items.append((uname, text))
        _seen_slack_ts = max_ts
        return new_items
    except Exception as e:
        log.error(f"Slack monitor check: {e}")
        return []
