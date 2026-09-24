"""
Розпізнавання мови: Whisper (Groq) → Google → Vosk, фільтр галюцинацій,
listen(), що не пише мікрофон під час власної озвучки.
"""
import json
import logging
import os
import re
import subprocess
import tempfile
import time

import speech_recognition as sr
from groq import Groq

from raphael import runtime
from raphael import settings as cfg
from raphael import tts
from raphael import usage

log = logging.getLogger("Лін")


def _make_groq_client(key: str):
    """Клієнт для Whisper. Таймаут короткий і без повторів SDK: за замовчуванням
    це 60 с × 3 спроби на кожну фразу, а за Whisper і так є Google і Vosk."""
    return Groq(api_key=key, timeout=15.0, max_retries=0)


client = _make_groq_client(cfg.GROQ_API_KEY)


def apply_settings():
    """Після config.json: ключ Groq і поріг паузи могли змінитись."""
    global client
    client = _make_groq_client(cfg.GROQ_API_KEY)
    recognizer.pause_threshold = float(cfg.PAUSE_THRESHOLD)

recognizer = sr.Recognizer()
recognizer.pause_threshold        = cfg.PAUSE_THRESHOLD   # тиша перед кінцем фрази
recognizer.non_speaking_duration  = 0.5   # мінімальний час тиші для кінця
recognizer.dynamic_energy_threshold = True

# ============================================================
#  STT
# ============================================================

# Підказки для Whisper — часто вживані слова, щоб краще розпізнавав
_WHISPER_PROMPT = (
    "Рафаель, Рафа, Лін, відкрий, закрий, відкрити, закрити, Spotify, спотіфай, Discord, дискорд, "
    "Chrome, хром, Telegram, телеграм, VS Code, Blender, погода, таймер, нагадай, "
    "нотатки, відтворити, пауза, наступний, попередній, гучніше, тихіше, "
    "скріншот, заблокуй, вимкни, перезавантаж, що грає, буфер, скопіюй"
)

# Відомі галюцинації Whisper на тиші/музиці/шумі (субтитри з YouTube).
# Коли немає мови, модель «домальовує» саме ці фрази — ігноруємо їх.
_WHISPER_HALLUCINATIONS = {
    "дякую", "дякую за перегляд", "дякую за увагу", "дякуємо за перегляд",
    "дякую що дивитесь", "субтитри", "субтитрував", "субтитри створив",
    "субтитри від", "редактор субтитрів", "продовження далі", "продовження буде далі",
    "продовження слідує", "далі буде", "до зустрічі", "до нових зустрічей",
    "побачимось", "ще побачимось", "на все добре", "на цьому все",
    "підписуйтесь на канал", "підписуйтесь", "не забудьте підписатися",
    # рос. варіанти (Whisper плутає мови на шумі)
    "спасибо за просмотр", "продолжение следует", "субтитры", "субтитры сделал",
    "редактор субтитров", "спасибо за внимание", "подписывайтесь на канал",
}

_VOWELS = set("аеиоуяюєїіыэё")


def _is_noise(text: str) -> bool:
    """True якщо текст — галюцинація Whisper або суцільний шум (не команда)."""
    t = text.strip().strip("!?.,…\"'» «").lower()
    if not t:
        return True
    if t in _WHISPER_HALLUCINATIONS:
        return True
    compact = t.replace(" ", "")
    # Одна літера повторена ('шшшшшш', 'аааа') — будь-якої довжини
    if len(set(compact)) == 1 and len(compact) >= 3:
        return True
    # Короткий уривок без жодної голосної — майже завжди шум ('сьш', 'кхм', 'тлс', 'м-м-м', 'шш').
    # Реальні слова/вигуки (стоп, грай, далі, так, ні, ну, ок, ага) голосну містять — не чіпаємо.
    letters = re.sub(r"[^а-яёіїєґ']", "", compact)
    if 0 < len(letters) <= 5 and not (set(letters) & _VOWELS):
        return True
    return False


def _transcribe_whisper(audio) -> str:
    """Groq Whisper — набагато краща розпізнавання української ніж Google STT."""
    try:
        import io
        wav_bytes = audio.get_wav_data()
        result = client.audio.transcriptions.create(
            file=("audio.wav", io.BytesIO(wav_bytes)),
            model="whisper-large-v3-turbo",   # 28 800 сек/день — вистачить
            language="uk",
            response_format="text",
            prompt=_WHISPER_PROMPT,
            temperature=0.0,   # детерміновано — менше «домальованих» фраз на шумі
        )
        usage.record("groq:whisper-large-v3-turbo",
                     seconds=len(audio.get_raw_data()) / (audio.sample_rate * audio.sample_width))
        text = (result or "").strip().lower()
        if _is_noise(text):
            log.debug(f"Whisper галюцинація/шум — ігнорую: '{text}'")
            return ""
        if text:
            log.debug(f"Whisper STT: '{text}'")
        return text
    except Exception as e:
        log.warning(f"Whisper STT failed, fallback to Google: {e}")
        return ""


def _transcribe_google(audio) -> str:
    """Google STT — резерв якщо Whisper не відповів."""
    try:
        text = recognizer.recognize_google(audio, language="uk-UA").lower()
        if _is_noise(text):
            log.debug(f"Google шум — ігнорую: '{text}'")
            return ""
        log.debug(f"Google STT (fallback): '{text}'")
        return text
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        log.warning(f"Google STT failed: {e}")
        return ""


# ── Vosk: офлайн-розпізнавання (працює без інтернету) ─────────────────────────
VOSK_MODEL_PATH = os.path.join(cfg.SCRIPT_DIR, "vosk-model-uk")
_vosk_model = None
_vosk_failed = False   # якщо модель не завантажилась — не пробуємо знову (інакше спам у логах)


def _get_vosk_model():
    """Лінива ініціалізація Vosk-моделі (один раз). Провал кешується."""
    global _vosk_model, _vosk_failed
    if _vosk_model is not None:
        return _vosk_model
    if _vosk_failed:
        return None

    path = os.environ.get("LIN_VOSK_PATH") or VOSK_MODEL_PATH
    if not os.path.isdir(path):
        _vosk_failed = True
        log.warning(f"Vosk: модель не знайдено ({path}) — офлайн STT вимкнено")
        return None

    # Vosk/Kaldi (C++) не відкриває не-ASCII шляхи на Windows (тут «Документи»).
    # Якщо в шляху є кирилиця — робимо junction на ASCII-локацію (один раз).
    if not path.isascii():
        try:
            ascii_dir = os.path.join(
                os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "lin-vosk")
            if not os.path.isdir(ascii_dir):
                subprocess.run(["cmd", "/c", "mklink", "/J", ascii_dir, path],
                               capture_output=True, text=True, timeout=10)
            if os.path.isdir(ascii_dir) and ascii_dir.isascii():
                log.info(f"Vosk: ASCII-junction {ascii_dir} → модель")
                path = ascii_dir
        except Exception as e:
            log.debug(f"Vosk junction не створено: {e}")

    try:
        from vosk import Model
        _vosk_model = Model(path)
        log.info("Vosk модель завантажена (офлайн STT готовий)")
        return _vosk_model
    except Exception as e:
        _vosk_failed = True   # більше не пробуємо до перезапуску
        log.error(f"Vosk init (офлайн STT вимкнено до рестарту): {e}")
        return None


def _transcribe_vosk(audio) -> str:
    """Офлайн-розпізнавання через Vosk — останній резерв коли немає інтернету."""
    model = _get_vosk_model()
    if not model:
        return ""
    try:
        import json as _json
        from vosk import KaldiRecognizer
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        rec = KaldiRecognizer(model, 16000)
        rec.AcceptWaveform(raw)
        res = _json.loads(rec.FinalResult())
        text = (res.get("text") or "").strip().lower()
        if _is_noise(text):
            return ""
        if text:
            log.debug(f"Vosk STT (офлайн): '{text}'")
        return text
    except Exception as e:
        log.warning(f"Vosk STT failed: {e}")
        return ""


def _wait_tts_quiet():
    """Чекає, доки замовкне озвучка (плюс хвіст на відлуння кімнати)."""
    while tts._tts_active.is_set() or time.monotonic() - tts._tts_last_end < tts.TTS_ECHO_TAIL:
        time.sleep(0.05)


_mic_calibrated = False   # калібруємо мікрофон лише раз


def _transcribe(audio) -> str:
    """
    Whisper (найкраще) → Google → Vosk (офлайн, коли немає інтернету).

    Розпізнаний текст пишеться в лог лише як DEBUG, тобто в lin.log не
    потрапляє: у звичайному режимі сюди приходить усе, що сказали поруч.
    Звернене до Рафаеля логують assistant і confirm («Команда: ...»).
    """
    return (_transcribe_whisper(audio)
            or _transcribe_google(audio)
            or _transcribe_vosk(audio))


def listen(timeout=30, phrase_limit=20, passive=False) -> str:
    """
    Слухає одну фразу і повертає текст ("" якщо тиша чи не розпізнано).

    Поки говорить сам Рафаель, мікрофон не пише, а запис, під час якого
    почалась озвучка з фонового потоку (новий лист, таймер, нагадування),
    викидається. Інакше він чув сам себе, а тему листа «Рафаель, вимкни
    компʼютер» сприймав би як команду.

    passive=True: фонове слухання в normal-режимі, коли чекаємо імʼя. Стан
    вікна не змінюємо: «Слухаю» світиться лише тоді, коли Рафаель справді
    чекає команду, і на екран не виводиться все, що сказали поруч.
    """
    global _mic_calibrated
    show = bool(runtime.LIN_UI) and not passive
    if show:
        runtime.LIN_UI.safe_set_state("listening")
    try:
        while True:
            _wait_tts_quiet()
            epoch = tts._tts_epoch
            with sr.Microphone() as source:
                # Калібрування лише першого разу — далі dynamic_energy_threshold
                # сам підлаштовується. Це прибирає ~150мс затримки на кожній команді.
                if not _mic_calibrated:
                    recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    _mic_calibrated = True
                try:
                    audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
                except sr.WaitTimeoutError:
                    log.debug("STT: тайм-аут")
                    if show: runtime.LIN_UI.safe_set_state("idle")
                    return ""
            if tts._tts_active.is_set() or tts._tts_epoch != epoch:
                log.debug("STT: запис наклався на озвучку, відкидаю і слухаю знову")
                continue
            break

        text = _transcribe(audio)

        if not text:
            log.debug("STT: не розпізнано")
            if show: runtime.LIN_UI.safe_set_state("idle")
            return ""

        if show:
            runtime.LIN_UI.safe_set_state("thinking", text)
        return text

    except Exception as e:
        log.error(f"listen() помилка: {e}", exc_info=True)
        if show: runtime.LIN_UI.safe_set_state("idle")
        time.sleep(1)   # без мікрофона цикл інакше крутився б без паузи, забиваючи лог
        return ""


# ── Локальний детектор імені (WAKE_ENGINE = "vosk") ──────────────────────────
# Зі звичайним двигуном ("whisper") кожна фраза, почута поруч (телевізор,
# музика, дзвінок), іде в Groq Whisper лише для того, щоб перевірити, чи
# немає в ній імені. Це і квота, і приватність. Тут імʼя шукає Vosk на цьому
# компʼютері: він безперервно слухає мікрофон з граматикою з самих імен, і
# лише коли фраза закінчилась і в ній прозвучало імʼя, аудіо саме цієї фрази
# йде в Whisper за повним текстом. Остаточно імʼя все одно перевіряє
# voice_rules.find_wake на тексті від Whisper, тож хибне спрацювання Vosk коштує лише
# один запит.
WAKE_CHUNK = 4000              # 0.25 с аудіо на 16 кГц
WAKE_MAX_UTTERANCE = 20        # секунд: з довшої фрази лишається кінець
_wake_vocab = None             # імена, які є в словнику моделі (кешується)


def _vosk_wake_words():
    """Імена зі словника моделі Vosk. None: локальний детектор недоступний."""
    global _wake_vocab
    model = _get_vosk_model()
    if not model:
        return None
    if _wake_vocab is None:
        try:
            _wake_vocab = sorted(w for w in cfg.WAKE_WORDS if model.find_word(w) != -1)
        except Exception as e:
            log.error(f"Локальний детектор імені: словник моделі недоступний: {e}")
            _wake_vocab = []
        if _wake_vocab:
            log.info(f"Локальний детектор імені: слухаю {_wake_vocab}")
        else:
            log.warning("Локальний детектор імені: жодного імені немає в словнику моделі "
                        "Vosk, лишаюсь на Whisper")
    return _wake_vocab or None


def wait_for_wake(timeout=30, interrupt=None):
    """
    Чекає звертання до Рафаеля, слухаючи мікрофон локально (Vosk).

    → текст фрази з імʼям (від Whisper/Google/Vosk, як у listen());
      "" якщо за timeout звертання не було або interrupt() попросив зупинитись;
      None якщо локальний детектор недоступний (немає моделі чи імен у словнику,
      не відкрився мікрофон), і тоді треба слухати по-старому через listen().
    """
    names = _vosk_wake_words()
    if not names:
        return None
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(_get_vosk_model(), 16000,
                          json.dumps(names + ["[unk]"], ensure_ascii=False))
    wanted = set(names)
    limit = WAKE_MAX_UTTERANCE * 16000 * 2
    utterance = bytearray()
    heard_name = False
    epoch = tts._tts_epoch
    deadline = time.monotonic() + timeout
    try:
        with sr.Microphone(sample_rate=16000, chunk_size=WAKE_CHUNK) as source:
            while time.monotonic() < deadline:
                if interrupt and interrupt():
                    return ""
                data = source.stream.read(WAKE_CHUNK)
                # Поки звучить сам Рафаель (і трохи після), мікрофон не слухаємо:
                # фраза, в яку втрутилась озвучка, починається заново
                if (tts._tts_active.is_set() or tts._tts_epoch != epoch
                        or time.monotonic() - tts._tts_last_end < tts.TTS_ECHO_TAIL):
                    rec.Reset()
                    utterance.clear()
                    heard_name, epoch = False, tts._tts_epoch
                    continue
                utterance += data
                if len(utterance) > limit:
                    del utterance[:len(utterance) - limit]
                if rec.AcceptWaveform(bytes(data)):
                    said = set(json.loads(rec.Result()).get("text", "").split())
                    if said & wanted:
                        log.debug(f"Локальний детектор: імʼя {sorted(said & wanted)}, фраза в розпізнавання")
                        return _transcribe(sr.AudioData(bytes(utterance), 16000, 2)) or ""
                    utterance.clear()
                    heard_name = False
                elif not heard_name:
                    partial = set(json.loads(rec.PartialResult()).get("partial", "").split())
                    if partial & wanted:
                        heard_name = True            # «Слухаю» з першим звуком імені
                        if runtime.LIN_UI:
                            runtime.LIN_UI.safe_set_state("listening")
        return ""
    except Exception as e:
        log.error(f"Локальний детектор імені: {e}", exc_info=True)
        time.sleep(1)
        return None
