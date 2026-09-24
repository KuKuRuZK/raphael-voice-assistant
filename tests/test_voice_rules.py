"""Тести розбору голосових фраз. Кожен кейс тут колись був реальним багом."""
from datetime import datetime

import pytest

import voice_rules as vr

WAKE = {"лін", "лин", "lin", "рафаель", "рафа", "рафаелю", "raphael", "лінь", "ліна"}


# ── Імʼя ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "через пʼять хвилин буду",          # «лин» усередині «хвилин»
    "подивись на ту лінію",             # «лін» усередині «лінію»
    "я був у берліні",
    "це фотографа робота",              # «рафа» усередині «фотографа»
    "Таймер 5 хвилин, час!",            # власна фраза Рафаеля
    "я сьогодні говорив з рафою і з колегою про звіт",   # імʼя не на початку й не в кінці
])
def test_wake_not_inside_words(text):
    assert vr.find_wake(text, WAKE) is None


@pytest.mark.parametrize("text,command", [
    ("рафа, увімкни музику", "увімкни музику"),
    ("Рафаель яка погода", "яка погода"),
    ("слухай рафа що там по пошті", "слухай що там по пошті"),
    ("увімкни музику, рафа", "увімкни музику"),
    ("рафа", ""),
])
def test_wake_found(text, command):
    found = vr.find_wake(text, WAKE)
    assert found is not None and found[1] == command


def test_strip_wake_words_keeps_other_words():
    assert vr.strip_wake_words("рафа режим розмови через хвилину", WAKE) == "режим розмови через хвилину"


# ── «Повтори» ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["повтори", "Повтори, будь ласка", "не почула", "скажи ще раз",
                                  "що ти сказала?", "ще раз"])
def test_repeat(text):
    assert vr.is_repeat_request(text)


@pytest.mark.parametrize("text", ["повторюй трек", "вимкни повтор", "повторюй плейлист",
                                  "спробуй ще раз увімкнути музику", "увімкни ще раз цю пісню"])
def test_not_repeat(text):
    assert not vr.is_repeat_request(text)


# ── Миттєві команди ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,action", [
    ("пауза", "spotify_pause"), ("Постав на паузу, будь ласка!", "spotify_pause"),
    ("наступний трек", "spotify_next"), ("гучніше", "volume_up"),
    ("котра година?", "say_time"), ("яка погода", "weather"),
])
def test_instant(text, action):
    assert vr.instant_command(text) == action


@pytest.mark.parametrize("text", ["пауза в роботі була довга", "яка погода в берліні",
                                  "наступний лист прочитай", "зроби гучніше в spotify"])
def test_instant_only_exact(text):
    assert vr.instant_command(text) is None


def test_cancel_shutdown():
    assert vr.is_cancel_shutdown("Рафа, скасуй вимкнення")
    assert vr.is_cancel_shutdown("не вимикай")
    assert not vr.is_cancel_shutdown("вимкни компʼютер")


# ── Диктування ────────────────────────────────────────────────────────────────
def test_dictation_exit():
    assert vr.is_dictation_exit("стоп")
    assert vr.is_dictation_exit("ну все, стоп диктування")
    assert not vr.is_dictation_exit("це кінець тижня")
    assert not vr.is_dictation_exit("дай стопку паперу")


def test_voice_punctuation():
    assert vr.apply_voice_punctuation("привіт кома як справи знак питання") == "привіт, як справи?"
    # «кома» усередині слова не чіпаємо
    assert vr.apply_voice_punctuation("команда готова крапка") == "команда готова."
    assert vr.apply_voice_punctuation("раз крапка з комою два") == "раз; два"


# ── Програми ──────────────────────────────────────────────────────────────────
KEYS = ["код", "vscode", "клод", "claude", "claude code", "клод код", "хром"]


@pytest.mark.parametrize("name,key", [
    ("клод код", "клод код"), ("claude code", "claude code"), ("відкрий клод код", "клод код"),
    ("клод", "клод"), ("код", "код"), ("хром", "хром"),
])
def test_app_key(name, key):
    assert vr.match_app_key(name, KEYS) == key


def test_app_key_not_inside_word():
    assert vr.match_app_key("штрихкод", KEYS) is None


# ── Відмінки ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n,word", [(1, "лист"), (2, "листи"), (4, "листи"), (5, "листів"),
                                    (11, "листів"), (12, "листів"), (14, "листів"),
                                    (21, "лист"), (22, "листи"), (25, "листів"), (111, "листів")])
def test_plural(n, word):
    assert vr.plural(n, "лист", "листи", "листів") == word


# ── Час нагадування ───────────────────────────────────────────────────────────
NOW = datetime(2026, 9, 24, 20, 0)


@pytest.mark.parametrize("value,expected", [
    ("21:00", "2026-09-24 21:00"),
    ("9:30", "2026-09-25 09:30"),           # уже минуло → завтра
    ("21.15", "2026-09-24 21:15"),
    ("30", "2026-09-24 20:30"),
    ("2026-09-26 09:00", "2026-09-26 09:00"),
    ("2026-09-20 09:00", None),             # минула дата
    ("25:00", None),                        # раніше тут був виняток
    ("завтра", None),
    ("", None),
])
def test_parse_remind_time(value, expected):
    assert vr.parse_remind_time(value, NOW) == expected


def test_describe_remind():
    assert vr.describe_remind("2026-09-24 21:00", NOW) == "о 21:00"
    assert vr.describe_remind("2026-09-25 09:00", NOW) == "завтра о 09:00"
    assert vr.describe_remind("2026-09-28 10:00", NOW) == "28.09 о 10:00"
