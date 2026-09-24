"""
Озвучка: edge-tts, кеш коротких фраз, швидкість голосу.

Поки грає озвучка, піднятий _tts_active, і stt.listen() мікрофон не пише.
"""
import asyncio
import hashlib
import logging
import os
import queue
import re
import tempfile
import threading
import time

import edge_tts
import pygame

from raphael import runtime
from raphael import settings as cfg

log = logging.getLogger("Лін")


_last_spoken = ""   # остання фраза Лін — для команди "повтори"
# Вихід з диктування і голосова пунктуація: voice_rules.is_dictation_exit
# та voice_rules.apply_voice_punctuation (цілими словами, з тестами).

# ── Динамічна швидкість голосу ────────────────────────────────────────────────
_VOICE_RATE_VALUE = 25   # поточний відсоток (ціле число)
_VOICE_RATE_MIN   = -20
_VOICE_RATE_MAX   =  60
_VOICE_RATE_STEP  =  10
_tts_lock  = threading.Lock()
_tts_stop  = threading.Event()   # встановити → зупинити TTS достроково
# Щоб Рафаель не чув сам себе: listen() не пише, поки щось звучить, і відкидає
# запис, під час якого почалась озвучка (фонові потоки говорять коли завгодно).
_tts_active   = threading.Event()   # зараз грає озвучка
_tts_epoch    = 0                   # лічильник озвучок, росте на кожній
_tts_last_end = 0.0                 # monotonic-час, коли озвучка востаннє замовкла
TTS_ECHO_TAIL = 0.35                # стільки секунд після озвучки ще не слухаємо (відлуння)
pygame.mixer.pre_init(44100, -16, 2, 512)
pygame.mixer.init()


# ============================================================
#  ШВИДКІСТЬ ГОЛОСУ
# ============================================================

def apply_settings():
    """Після config.json: відсоток швидкості під VOICE_RATE, щоб «швидше» і
    «повільніше» рахувались від налаштованої швидкості, а не від +25%."""
    global _VOICE_RATE_VALUE
    try:
        _VOICE_RATE_VALUE = int(str(cfg.VOICE_RATE).replace("%", "").replace("+", ""))
    except ValueError:
        log.error(f"VOICE_RATE не схожий на відсоток: {cfg.VOICE_RATE!r}")


def _adjust_voice_rate(direction: str) -> str:
    """Змінює швидкість TTS. direction: faster / slower / reset"""
    global _VOICE_RATE_VALUE
    if direction == "faster":
        _VOICE_RATE_VALUE = min(_VOICE_RATE_VALUE + _VOICE_RATE_STEP, _VOICE_RATE_MAX)
    elif direction == "slower":
        _VOICE_RATE_VALUE = max(_VOICE_RATE_VALUE - _VOICE_RATE_STEP, _VOICE_RATE_MIN)
    else:
        _VOICE_RATE_VALUE = 25
    sign = "+" if _VOICE_RATE_VALUE >= 0 else ""
    cfg.VOICE_RATE = f"{sign}{_VOICE_RATE_VALUE}%"
    log.info(f"Voice rate: {cfg.VOICE_RATE}")
    if direction == "reset":
        return "Швидкість повернута до нормальної."
    return f"{'Швидше' if direction == 'faster' else 'Повільніше'}. Зараз {cfg.VOICE_RATE}."


# ============================================================
#  TTS
# ============================================================

_MD_RE = [
    (re.compile(r"```.*?```", re.S), " "),        # блоки коду не читаємо взагалі
    (re.compile(r"`([^`]*)`"), r"\1"),            # `код` → код
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),      # **жирне** → жирне
    (re.compile(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)"), r"\1"),   # *курсив* → курсив
    (re.compile(r"^#{1,6}\s*", re.M), ""),        # заголовки
    (re.compile(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]"), r"\1"),    # [[вікі-посилання]]
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),           # [текст](посилання)
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),      # маркери списків
    (re.compile(r"[ \t]{2,}"), " "),
]


def _strip_md(text: str) -> str:
    """
    Прибирає розмітку перед озвученням. Модель часто відповідає у Markdown,
    і зірочки з лапками або читаються вголос, або калічать інтонацію.
    На екран текст іде як є, чиститься лише те, що йде в голос.
    """
    for rx, rep in _MD_RE:
        text = rx.sub(rep, text)
    return text.strip()


# ── Кеш коротких фраз ─────────────────────────────────────────────────────────
# «Слухаю.», «Скасовую.», привітання звучать десятки разів на день, і щоразу
# чекати синтез edge-tts (запит у мережу) немає сенсу. Короткі фрази лежать
# готовими mp3. Тека поза проєктом, щоб не синхронізувалась в OneDrive.
TTS_CACHE_DIR       = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                                   "raphael-tts-cache")
TTS_CACHE_MAX_CHARS = 80
TTS_CACHE_MAX_FILES = 400


def _tts_cache_path(text: str) -> str | None:
    if len(text) > TTS_CACHE_MAX_CHARS:
        return None
    key = hashlib.sha1(f"{cfg.VOICE}|{cfg.VOICE_RATE}|{cfg.VOICE_PITCH}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(TTS_CACHE_DIR, key + ".mp3")


def _tts_cache_trim():
    """Лишає TTS_CACHE_MAX_FILES найсвіжіших файлів (за часом останнього використання)."""
    try:
        files = [os.path.join(TTS_CACHE_DIR, f) for f in os.listdir(TTS_CACHE_DIR)
                 if f.endswith(".mp3")]
        if len(files) <= TTS_CACHE_MAX_FILES:
            return
        files.sort(key=os.path.getmtime)
        for p in files[:len(files) - TTS_CACHE_MAX_FILES + 50]:
            os.remove(p)
    except Exception as e:
        log.debug(f"TTS кеш: {e}")


def _tts_synth_to(path: str, text: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(edge_tts.Communicate(
            text, voice=cfg.VOICE, rate=cfg.VOICE_RATE, pitch=cfg.VOICE_PITCH).save(path))
    finally:
        loop.close()


def _tts_play(path: str):
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    clock = pygame.time.Clock()
    while pygame.mixer.music.get_busy():
        if _tts_stop.is_set():
            pygame.mixer.music.stop()
            log.debug("TTS: зупинено користувачем")
            break
        clock.tick(10)
    pygame.mixer.music.unload()


def _tts_audio(text: str) -> tuple:
    """mp3 для фрази: з кешу або щойно синтезований. → (шлях, чи тимчасовий файл)."""
    cached = _tts_cache_path(text)
    if cached and os.path.exists(cached):
        try:
            os.utime(cached, None)          # свіжий для _tts_cache_trim
        except Exception:
            pass
        return cached, False
    if cached:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        part = cached + ".part"             # недописаний файл не потрапить у кеш
        try:
            _tts_synth_to(part, text)
            os.replace(part, cached)
        finally:
            if os.path.exists(part):
                os.unlink(part)
        _tts_cache_trim()
        return cached, False
    fd, tmp = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        _tts_synth_to(tmp, text)
    except Exception:
        os.unlink(tmp)
        raise
    return tmp, True


# ── Озвучка по реченнях ───────────────────────────────────────────────────────
# Раніше весь текст синтезувався цілком і лише потім починав звучати: на
# довгих фразах (брифінг, список листів, подій) це секунда-дві тиші. Тепер
# текст ріжеться на речення, і наступне синтезується, поки звучить поточне.
# Те саме працює для відповіді моделі, що приходить потоком (llm.llm_stream):
# перше речення звучить, поки модель ще пише решту.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")
TTS_MIN_CHUNK = 80      # дрібні речення після першого склеюються до такої довжини


def _drop_code_blocks(chunks):
    """Прибирає ```блоки коду``` з потоку тексту: код уголос не читаємо."""
    in_code = False
    for chunk in chunks:
        parts = (chunk or "").split("```")
        out = []
        for i, part in enumerate(parts):
            if i:
                in_code = not in_code
                out.append(" ")
            if not in_code:
                out.append(part)
        yield "".join(out)


def _sentences(chunks):
    """
    Шматки тексту (рядок або потік від моделі) → фрази для синтезу.
    Перше речення віддається одразу, щоб швидше зазвучало; наступні
    дрібні склеюються до TTS_MIN_CHUNK, щоб не робити зайвих запитів.
    """
    buf, pending, first = "", "", True
    for chunk in _drop_code_blocks(chunks):
        buf += chunk
        while True:
            m = _SENTENCE_END.search(buf)
            if not m:
                break
            piece, buf = buf[:m.start()].strip(), buf[m.end():]
            if not piece:
                continue
            pending = f"{pending} {piece}" if pending else piece
            if first or len(pending) >= TTS_MIN_CHUNK:
                yield pending
                pending, first = "", False
    rest = f"{pending} {buf.strip()}".strip()
    if rest:
        yield rest


def speak_stream(chunks) -> str:
    """
    Говорить текст, що надходить шматками: генератор відповіді моделі або
    список рядків. Наступне речення синтезується, поки грає поточне.
    Повертає все, що прозвучало. Якщо джерело впало раніше, ніж щось
    прозвучало (модель не відповіла), виняток перекидається тому, хто
    викликав, щоб той міг сказати «не вдалося».
    """
    global _last_spoken, _tts_epoch, _tts_last_end
    _tts_stop.clear()
    spoken, failure = [], []
    ready = queue.Queue(maxsize=2)      # синтез щонайбільше на 2 речення наперед

    def _produce():
        try:
            for sentence in _sentences(chunks):
                if _tts_stop.is_set():
                    break
                clean = _strip_md(sentence)
                if not clean:
                    continue
                try:
                    audio = _tts_audio(clean)
                except Exception as e:
                    # Найчастіше це немає інтернету: решту речень теж не
                    # синтезуємо, щоб не чекати таймаут на кожному
                    log.error(f"TTS помилка: {e}", exc_info=True)
                    break
                spoken.append(clean)
                ready.put((clean, audio))
        except Exception as e:
            failure.append(e)
            log.error(f"TTS: джерело тексту обірвалось: {e}")
        finally:
            ready.put(None)

    with _tts_lock:  # один потік говорить за раз
        producer = threading.Thread(target=_produce, daemon=True)
        producer.start()
        playing = False
        try:
            while True:
                item = ready.get()
                if item is None:
                    break
                text, (path, temporary) = item
                try:
                    if _tts_stop.is_set():
                        continue
                    if not playing:
                        # Лічильник і прапорець піднімаються з першим звуком, а не
                        # з початком синтезу: listen() відкидає лише запис, під
                        # час якого справді щось звучало
                        _tts_epoch += 1
                        _tts_active.set()
                        playing = True
                    log.debug(f"TTS: '{text[:50]}...' " if len(text) > 50 else f"TTS: '{text}'")
                    if runtime.LIN_UI:
                        runtime.LIN_UI.safe_set_state("speaking", text)
                    try:
                        _tts_play(path)
                    except Exception as e:
                        # Звук зламався: решту не граємо, але чергу дочитуємо,
                        # щоб потік синтезу не завис на повній черзі
                        log.error(f"TTS: програвання не вдалося: {e}", exc_info=True)
                        _tts_stop.set()
                finally:
                    if temporary:
                        try:
                            os.unlink(path)
                        except Exception:
                            pass
            producer.join(timeout=5)
            log.debug("TTS: завершено")
        finally:
            if playing:
                _tts_last_end = time.monotonic()
                _tts_active.clear()
    if runtime.LIN_UI:
        runtime.LIN_UI.safe_set_state("idle")
    text = " ".join(spoken)
    if text:
        _last_spoken = text   # зберігаємо для "повтори"
    elif failure:
        raise failure[0]
    return text


def speak(text: str):
    if not text or not text.strip():
        return
    speak_stream([text])
