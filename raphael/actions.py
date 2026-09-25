"""
Виконання дій, які просить модель: [ACTION:тип:параметр] → потрібний модуль.
"""
import difflib
import logging
import os
import subprocess
import threading
import time
import urllib.parse

import pyautogui

from raphael import agenda
from raphael import brain
from raphael import claude_code
from raphael import dictation
from raphael import gmail
from raphael import monitor
from raphael import music
from raphael import notes
from raphael import pc
from raphael import runtime
from raphael import settings as cfg
from raphael import slack_chat
from raphael import sysmon
from raphael import timers
from raphael import tts
from raphael import usage
from raphael import voice_rules as vr
from raphael import web

log = logging.getLogger("Лін")


def execute_action(action_str: str):
    try:
        inner = action_str.strip("[]").replace("ACTION:", "")
        parts = inner.split(":", 1)
        t = parts[0]
        p = parts[1].strip() if len(parts) > 1 else ""

        if   t == "open_app":          pc.open_application(p)
        elif t == "search_web":
            # quote_plus: без нього «C# tutorial» Google отримував як «C»
            url = f"https://www.google.com/search?q={urllib.parse.quote_plus(p)}"
            pc._open_url(url)
            log.info(f"Chrome search: {url}")
        elif t == "open_youtube":
            url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(p)}"
                   if p else "https://youtube.com")
            pc._open_url(url)
            log.info(f"Chrome YouTube: {url}")
        elif t == "open_url":
            url = p if p.startswith("http") else f"https://{p}"
            pc._open_url(url)
            log.info(f"Chrome URL: {url}")
        elif t == "open_folder":
            folder_aliases = {
                "скріншот": cfg.SCREENSHOT_DIR, "скріншоти": cfg.SCREENSHOT_DIR,
                "робочий стіл": cfg.SCREENSHOT_DIR, "десктоп": cfg.SCREENSHOT_DIR,
                "документи": os.path.join(os.path.expanduser("~"), "Documents"),
                "завантаження": os.path.join(os.path.expanduser("~"), "Downloads"),
                "музика": os.path.join(os.path.expanduser("~"), "Music"),
                "відео": os.path.join(os.path.expanduser("~"), "Videos"),
                "зображення": os.path.join(os.path.expanduser("~"), "Pictures"),
            }
            target = next(
                (v for k, v in folder_aliases.items() if k in p.lower()),
                p if (p and os.path.exists(p)) else os.path.expanduser("~")
            )
            subprocess.Popen(["explorer", target])
        elif t == "get_time":          pass
        elif t == "volume_up":
            # Якщо Spotify грає — змінюємо його гучність, а не системну
            if not music._maybe_spotify_volume("up"):
                pc.volume_control("up")
        elif t == "volume_down":
            if not music._maybe_spotify_volume("down"):
                pc.volume_control("down")
        elif t == "volume_mute":       pc.volume_control("mute")
        elif t == "screenshot":
            path = pc.take_screenshot()
            if path:
                tts.speak("Скріншот збережено.")
        elif t == "type_text":
            # pyautogui.write не підтримує Unicode — використовуємо clipboard
            import pyperclip as _pc
            _old = _pc.paste()
            _pc.copy(p)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
            _pc.copy(_old)   # відновлюємо буфер
        elif t == "hotkey":
            # Переклад українських назв клавіш в англійські
            uk_to_en = {
                "пробіл": "space", "ентер": "enter", "вхід": "enter",
                "вверх": "up", "вниз": "down", "вгору": "up",
                "ліво": "left", "право": "right",
                "ліворуч": "left", "праворуч": "right",
                "таб": "tab", "ескейп": "escape", "есс": "escape",
                "видалити": "delete", "назад": "backspace",
                "додому": "home", "кінець": "end",
                "плюс": "add", "мінус": "subtract",
            }
            keys = [uk_to_en.get(k.lower(), k.lower()) for k in p.split("+")]
            log.info(f"Hotkey: {keys}")
            pyautogui.hotkey(*keys)
        elif t == "close_window":      pyautogui.hotkey("alt", "F4")
        elif t == "minimize_all":      pyautogui.hotkey("win", "d")
        elif t == "focus_window":
            # Фокусує і розгортає вікно за назвою
            try:
                import pygetwindow as gw
                wins = [w for w in gw.getAllWindows() if p.lower() in w.title.lower() and w.title]
                if wins:
                    w = wins[0]
                    w.restore()
                    w.activate()
                    w.maximize()
                    log.info(f"Focused: {w.title}")
                else:
                    log.warning(f"Вікно '{p}' не знайдено")
            except Exception as e:
                log.error(f"focus_window помилка: {e}")
        elif t == "maximize_window":
            try:
                import pygetwindow as gw
                wins = [w for w in gw.getAllWindows() if p.lower() in w.title.lower() and w.title]
                if wins:
                    wins[0].maximize()
            except Exception as e:
                log.error(f"maximize_window помилка: {e}")
        elif t == "system_lock":       subprocess.Popen("rundll32.exe user32.dll,LockWorkStation")
        elif t == "system_sleep":
            subprocess.Popen(["powershell", "-c",
                "Add-Type -Assembly System.Windows.Forms; "
                "[System.Windows.Forms.Application]::SetSuspendState("
                "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"])
        # Вимкнення і ребут: після підтвердження в ask_lin і з запасом у 30 с,
        # за які можна сказати «скасуй вимкнення» (shutdown /a).
        elif t == "system_restart":
            subprocess.Popen(["shutdown", "/r", "/t", str(pc.SHUTDOWN_DELAY)])
            tts.speak(f"Перезавантажую через {pc.SHUTDOWN_DELAY} секунд. Скажи «скасуй вимкнення», щоб зупинити.")
        elif t == "system_shutdown_pc":
            subprocess.Popen(["shutdown", "/s", "/t", str(pc.SHUTDOWN_DELAY)])
            tts.speak(f"Вимикаю через {pc.SHUTDOWN_DELAY} секунд. Скажи «скасуй вимкнення», щоб зупинити.")
        elif t == "system_cancel_shutdown":
            pc._cancel_shutdown()
        elif t == "kill_process":
            n = pc.kill_process(p)
            tts.speak(f"Закрила {p}." if n == 1 else
                  f"Закрила {p}: {vr.count(n, 'процес', 'процеси', 'процесів')}." if n else
                  f"Не знайшла процес «{p}».")

        # ── SPOTIFY ──
        elif t == "spotify_play":
            if p:
                # Спочатку пробуємо через API (пошук + відтворення)
                sp = music._get_spotipy()
                played = False
                if sp:
                    try:
                        results = sp.search(q=p, type="track", limit=1)
                        tracks = results.get("tracks", {}).get("items", [])
                        if tracks:
                            sp.start_playback(uris=[tracks[0]["uri"]])
                            log.info(f"Spotify API play: {tracks[0]['name']}")
                            played = True
                    except Exception as e:
                        log.warning(f"Spotify API play failed: {e}")
                        if "No active device" in str(e):
                            tts.speak("Відкрий Spotify на телефоні або ПК, щоб я могла відтворити.")
                            return
                if not played:
                    # Fallback: URI схема
                    uri = "spotify:search:" + urllib.parse.quote(p)
                    os.startfile(uri)   # без shell — ShellExecute по протоколу spotify:
                    log.info(f"Spotify URI fallback: {uri}")
            else:
                music._spotify_set_playing(True)      # «грай» без назви = продовжити

        elif t == "spotify_pause":
            music._spotify_set_playing(False)

        elif t == "spotify_resume":
            music._spotify_set_playing(True)

        elif t == "spotify_next":
            sp = music._get_spotipy()
            if sp:
                try:
                    sp.next_track(); log.info("Spotify API: next")
                except Exception as e:
                    log.warning(f"Spotify next API fail: {e}"); pyautogui.hotkey("nexttrack")
            else:
                pyautogui.hotkey("nexttrack")

        elif t == "spotify_prev":
            sp = music._get_spotipy()
            if sp:
                try:
                    sp.previous_track(); log.info("Spotify API: prev")
                except Exception as e:
                    log.warning(f"Spotify prev API fail: {e}"); pyautogui.hotkey("prevtrack")
            else:
                pyautogui.hotkey("prevtrack")

        elif t == "spotify_stop":
            music._spotify_set_playing(False)

        elif t == "spotify_device":
            music._spotify_transfer_device(p)

        elif t == "spotify_devices":
            music._spotify_list_devices()

        elif t == "spotify_current":
            music._spotify_now_playing()

        elif t == "spotify_recent":
            n = int(p) if p.isdigit() else 5
            music._spotify_recent(n)

        elif t == "spotify_liked":
            n = int(p) if p.isdigit() else 5
            music._spotify_liked(n)

        elif t == "spotify_like":
            music._spotify_save_current(True)

        elif t == "spotify_unlike":
            music._spotify_save_current(False)

        elif t == "spotify_volume_up":
            step = int(p) if p.isdigit() else 10
            music._spotify_volume("up", step)

        elif t == "spotify_volume_down":
            step = int(p) if p.isdigit() else 10
            music._spotify_volume("down", step)

        elif t == "spotify_volume":
            # p = "50" → встановити 50%
            try:
                music._spotify_volume("set", int(p))
            except ValueError:
                tts.speak("Не зрозуміла рівень гучності.")

        elif t == "spotify_shuffle":
            # p = "on"/"off"/"toggle"
            if p in ("on", "увімкни", "так", "1", "true"):
                music._spotify_shuffle(True)
            elif p in ("off", "вимкни", "ні", "0", "false"):
                music._spotify_shuffle(False)
            else:
                # toggle — дивимось поточний стан
                sp2 = music._get_spotipy()
                if sp2:
                    try:
                        st = sp2.current_playback()
                        current = st.get("shuffle_state", False) if st else False
                        music._spotify_shuffle(not current)
                    except Exception:
                        music._spotify_shuffle(True)
                else:
                    music._spotify_shuffle(True)

        elif t == "spotify_repeat":
            # p = "track" | "context" | "off" | "трек" | "плейлист" тощо
            music._spotify_repeat(p or "off")

        elif t == "spotify_playlists":
            threading.Thread(target=music._spotify_playlists, daemon=True).start()

        elif t == "spotify_play_playlist":
            threading.Thread(
                target=lambda: music._spotify_play_playlist(p), daemon=True
            ).start()

        elif t == "spotify_queue":
            threading.Thread(target=music._spotify_queue, daemon=True).start()

        elif t == "spotify_playlist_tracks":
            threading.Thread(
                target=lambda: music._spotify_playlist_tracks(p), daemon=True
            ).start()

        elif t == "spotify_add_queue":
            threading.Thread(
                target=lambda: music._spotify_add_to_queue(p), daemon=True
            ).start()

        # ── НОТАТКИ ──
        elif t == "note_add":
            # формат p: "текст нотатки" або "текст|60" (з нагадуванням через 60 хв)
            parts_n = p.split("|", 1)
            txt = parts_n[0].strip()
            mins = int(parts_n[1]) if len(parts_n) > 1 and parts_n[1].strip().isdigit() else 0
            result = notes.note_add(txt, mins)
            tts.speak(result)
            return result

        elif t == "note_remind":
            # формат: "текст|30" (хвилини), "текст|21:00" (сьогодні/завтра)
            # або "текст|2026-09-26 09:00" (конкретна дата)
            parts_n = p.split("|", 1)
            txt = parts_n[0].strip()
            time_val = parts_n[1].strip() if len(parts_n) > 1 else "30"
            result = notes.note_add(txt, remind_at_str=time_val)
            tts.speak(result)
            return result

        elif t == "note_list":
            result = notes.note_list()
            tts.speak(result)
            return result

        elif t == "note_done":
            try:
                result = notes.note_done(int(p))
            except ValueError:
                result = "Невірний номер нотатки."
            tts.speak(result)
            return result

        elif t == "note_delete":
            try:
                result = notes.note_delete(int(p))
            except ValueError:
                result = "Невірний номер нотатки."
            tts.speak(result)
            return result

        elif t == "note_clear":
            result = notes.note_clear("done" if p.strip().lower() in ("done", "виконані", "виконане") else "all")
            tts.speak(result)
            return result

        # ── ДРУГИЙ МОЗОК ──
        elif t == "brain_add":
            result = brain.brain_capture(p)
            tts.speak(result)
            return result

        elif t == "brain_plan":
            result = brain.brain_capture(p, "плани")
            tts.speak(result)
            return result

        elif t == "brain_idea":
            result = brain.brain_capture(p, "ідеї")
            tts.speak(result)
            return result

        elif t == "brain_ask":
            brain.brain_ask(p)          # сам скаже відповідь, коли прочитає нотатки
            return ""

        elif t == "brain_read":
            result = brain.brain_read(p)
            tts.speak(result)
            return result

        elif t == "dictate_type":
            if p.strip():
                dictation._type_at_cursor(p.strip())    # текст названо одразу
            else:
                dictation._dictate_to_cursor()          # диктуватиме окремо
            return ""

        elif t == "dictate_last":
            # «що я диктував», «скопіюй те, що я диктував», «повтори диктовку»
            if not dictation._last_dictation:
                tts.speak("Я ще нічого не диктувала.")
                return ""
            try:
                import pyperclip
                pyperclip.copy(dictation._last_dictation)
            except Exception as e:
                log.error(f"dictate_last: {e}")
            tts.speak("Остання диктовка знову в буфері.")
            return dictation._last_dictation

        elif t == "dictate_to_brain":
            # «збережи диктовку в мозок» — якщо вставити було нікуди
            if not dictation._last_dictation:
                tts.speak("Я ще нічого не диктувала.")
                return ""
            result = brain.brain_capture(dictation._last_dictation)
            tts.speak(result if len(dictation._last_dictation) < 120 else "Зберегла у вхідні.")
            return result

        # ── ФОКУС ──
        elif t == "focus_start":
            mins = int(p) if p.isdigit() else 25
            result = timers.focus_start(mins)
            tts.speak(result)
            return result

        elif t == "focus_stop":
            result = timers.focus_stop()
            tts.speak(result)
            return result

        # ── ПАМ'ЯТЬ ──
        elif t == "memory_save_name":
            mem = notes.load_memory()
            mem["user_name"] = p
            notes.save_memory(mem)
            result = f"Запам'ятала — тебе звуть {p}."
            tts.speak(result)
            return result

        elif t == "memory_add_fact":
            mem = notes.load_memory()
            facts = mem.get("facts", [])
            facts.append(p)
            mem["facts"] = facts[-10:]  # зберігаємо останні 10 фактів
            notes.save_memory(mem)
            log.info(f"Факт збережено: {p}")
            return f"Запам'ятала: {p}."

        elif t == "notepad_write":
            pc._notepad_write(p)
            return

        # ── ТАЙМЕР ──
        elif t == "timer":
            # p може бути "600", "10m", "1.5m", "10:30"
            seconds = 0
            lp = p.strip().lower()
            try:
                if lp.endswith("m"):
                    seconds = round(float(lp[:-1]) * 60)
                elif lp.endswith("s"):
                    seconds = round(float(lp[:-1]))
                elif ":" in lp:
                    parts_t = lp.split(":")
                    seconds = int(parts_t[0]) * 60 + int(parts_t[1])
                else:
                    seconds = round(float(lp))
            except (ValueError, IndexError):
                seconds = 0
            if seconds > 0:
                result = timers._timer_start(seconds)
                tts.speak(result)
            else:
                tts.speak("Не зрозуміла скільки часу.")
            return

        elif t == "timer_stop":
            tts.speak(timers._timer_stop_all())
            return

        # ── БУФЕР ОБМІНУ ──
        elif t == "clipboard_read":
            threading.Thread(
                target=lambda: tts.speak(pc._clipboard_read()), daemon=True
            ).start()
            return

        elif t == "clipboard_save":
            tts.speak(pc._clipboard_save())
            return

        elif t == "clipboard_copy":
            tts.speak(pc._clipboard_set(p))
            return

        # ── ШВИДКІСТЬ ГОЛОСУ ──
        elif t == "voice_faster":
            tts.speak(tts._adjust_voice_rate("faster"))
            return

        elif t == "voice_slower":
            tts.speak(tts._adjust_voice_rate("slower"))
            return

        elif t == "voice_reset":
            tts.speak(tts._adjust_voice_rate("reset"))
            return

        # ── ВІКНА ──
        elif t == "window_snap":
            snap_map = {
                "left":      ("win", "left"),
                "ліво":      ("win", "left"),
                "ліворуч":   ("win", "left"),
                "right":     ("win", "right"),
                "право":     ("win", "right"),
                "праворуч":  ("win", "right"),
                "max":       ("win", "up"),
                "макс":      ("win", "up"),
                "повний":    ("win", "up"),
                "full":      ("win", "up"),
                "maximize":  ("win", "up"),
                "розгорнути":("win", "up"),
                "min":       ("win", "down"),
                "згорнути":  ("win", "down"),
                "minimize":  ("win", "down"),
            }
            key_val = p.lower().strip()
            keys = snap_map.get(key_val)
            if not keys:
                # fuzzy fallback
                close = difflib.get_close_matches(key_val, snap_map.keys(), n=1, cutoff=0.6)
                if close:
                    keys = snap_map[close[0]]
                    log.info(f"Window snap fuzzy: '{key_val}' → '{close[0]}'")
            if keys:
                pyautogui.hotkey(*keys)
                log.info(f"Window snap: {p}")
            else:
                log.warning(f"Невідомий snap: {p}")
            return

        # ── ВАЛЮТА ──
        elif t == "currency":
            # формат: "100:USD:UAH"
            parts_c = p.split(":")
            if len(parts_c) == 3:
                try:
                    amt = float(parts_c[0])
                    threading.Thread(
                        target=lambda: tts.speak(web._currency_convert(amt, parts_c[1], parts_c[2])),
                        daemon=True
                    ).start()
                except ValueError:
                    tts.speak("Не зрозуміла суму для конвертації.")
            else:
                tts.speak("Не зрозумів формат конвертації.")
            return

        # ── ПОГОДА ──
        # HTTP запит робиться паралельно; _tts_lock гарантує що погода
        # озвучується після того як AI-відповідь договорить
        elif t == "weather":
            _city = p if p else None
            threading.Thread(
                target=lambda: tts.speak(web._get_weather(_city)), daemon=True
            ).start()
            return

        # ── СТАН СИСТЕМИ ──
        elif t == "system_info":
            threading.Thread(
                target=lambda: tts.speak(sysmon._get_system_info()), daemon=True
            ).start()
            return

        elif t == "system_health":
            threading.Thread(
                target=lambda: tts.speak(sysmon._system_health_report()), daemon=True
            ).start()

        elif t == "usage_report":
            tts.speak(usage.report_text())
            return

        elif t == "web_search":
            web._silent_web_search(p)
            return

        elif t == "claude_session":
            summary = claude_code._read_claude_session(int(p) if p.isdigit() else 4)
            tts.speak(summary)
            return summary

        elif t == "claude_read":
            summary = claude_code._read_claude_session(n_messages=6, keyword=p)
            tts.speak(summary)
            return summary

        elif t == "claude_list":
            summary = claude_code._list_claude_sessions()
            tts.speak(summary)
            return summary

        elif t == "ask_claude":
            claude_code._ask_claude_code(p)

        elif t == "ask_claude_web":
            claude_code._ask_claude_web(p)

        # ── SLACK ──
        elif t == "slack_unread":
            threading.Thread(target=slack_chat._slack_unread, daemon=True).start()
            return

        elif t == "slack_dm":
            threading.Thread(target=slack_chat._slack_dm, daemon=True).start()
            return

        elif t == "slack_mentions":
            threading.Thread(target=slack_chat._slack_mentions, daemon=True).start()
            return

        elif t == "slack_channel":
            # p = назва каналу
            threading.Thread(
                target=lambda: slack_chat._slack_channel(p), daemon=True
            ).start()
            return

        # ── GMAIL ──
        elif t == "gmail_unread":
            threading.Thread(target=gmail._gmail_unread, daemon=True).start()
            return

        elif t == "gmail_latest":
            threading.Thread(target=gmail._gmail_latest, daemon=True).start()
            return

        elif t == "gmail_search":
            # p = пошуковий запит
            threading.Thread(
                target=lambda: gmail._gmail_search(p), daemon=True
            ).start()
            return

        elif t == "gmail_read_full":
            # p = опціональний запит (інакше останній непрочитаний)
            threading.Thread(
                target=lambda: gmail._gmail_read_full(p), daemon=True
            ).start()
            return

        elif t == "gmail_reply":
            # формат: "запит|текст відповіді"  або просто "текст" (на останній непрочитаний)
            if "|" in p:
                q, body = p.split("|", 1)
            else:
                q, body = "", p
            threading.Thread(
                target=lambda: gmail._gmail_draft_reply(q.strip(), body.strip()), daemon=True
            ).start()
            return

        # ── КАЛЕНДАР ──
        elif t == "calendar_today":
            threading.Thread(target=lambda: agenda._calendar_agenda("today"), daemon=True).start()
            return

        elif t == "calendar_tomorrow":
            threading.Thread(target=lambda: agenda._calendar_agenda("tomorrow"), daemon=True).start()
            return

        elif t == "calendar_week":
            threading.Thread(target=lambda: agenda._calendar_agenda("week"), daemon=True).start()
            return

        elif t == "calendar_create":
            # формат: "Назва|YYYY-MM-DD HH:MM|хвилини"
            parts_e = p.split("|")
            if len(parts_e) >= 2:
                summary = parts_e[0].strip()
                when    = parts_e[1].strip()
                mins    = int(parts_e[2]) if len(parts_e) > 2 and parts_e[2].strip().isdigit() else 60
                threading.Thread(
                    target=lambda: agenda._calendar_create(summary, when, mins), daemon=True
                ).start()
            else:
                tts.speak("Не зрозуміла деталі події.")
            return

        # ── ЕКРАН (VISION) ──
        elif t == "screen_look":
            # p = опціональне питання про екран
            threading.Thread(
                target=lambda: pc._screen_look(p), daemon=True
            ).start()
            return

        # ── МОНІТОРИНГ ──
        elif t == "monitor_on":
            monitor._monitor_toggle(p or "all", True)
            return

        elif t == "monitor_off":
            monitor._monitor_toggle(p or "all", False)
            return

        elif t == "shutdown":
            tts.speak("До побачення!")
            runtime._mark_stop()
            os._exit(0)

        log.info(f"Дія виконана: [{t}:{p}]")
    except Exception as e:
        log.error(f"Помилка дії '{action_str}': {e}", exc_info=True)
