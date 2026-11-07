"""Лін у Telegram (raphael/tg_lin.py): той самий мозок, відповіді туди, де Влад."""
import threading
import time

import pytest


@pytest.fixture
def chat(env, lin, monkeypatch):
    """Telegram без мережі: що бот надіслав, лежить у sent як (chat_id, текст, кнопки)."""
    from raphael import tg
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda chat_id, text, **kw: sent.append((chat_id, text, kw)))
    monkeypatch.setattr(tg, "api", lambda method, **kw: True)          # «друкує...», відповіді на кнопки
    return sent


@pytest.fixture
def brain(monkeypatch):
    from raphael import assistant
    got = []
    monkeypatch.setattr(assistant, "process_command", lambda text, channel="voice": got.append((text, channel)))
    return got


def msg(text=None, chat_id=111, **extra):
    m = {"message_id": 1, "from": {"id": chat_id}, "chat": {"id": chat_id}, **extra}
    if text is not None:
        m["text"] = text
    return m


def test_text_from_phone_goes_to_brain(chat, brain, env):
    from raphael import runtime, tg_lin
    tg_lin.handle(msg("Лін, яка погода"))
    assert brain == [("яка погода", "telegram")]
    assert runtime.TG_CHAT_ID == 111


def test_voice_note_is_heard_and_shown(chat, brain, monkeypatch):
    from raphael import stt, tg, tg_lin
    monkeypatch.setattr(tg, "download_file", lambda file_id: b"OggS...")
    monkeypatch.setattr(stt, "transcribe_file", lambda data, name="", seconds=0: "яка погода завтра")
    tg_lin.handle(msg(voice={"file_id": "abc", "duration": 2}))
    assert (111, "🎤 яка погода завтра", {}) in chat                # видно, що саме почула
    assert brain == [("яка погода завтра", "telegram")]


def test_voice_note_failure_is_said_honestly(chat, brain, monkeypatch):
    from raphael import tg, tg_lin
    monkeypatch.setattr(tg, "download_file", lambda file_id: (_ for _ in ()).throw(OSError("нема мережі")))
    tg_lin.handle(msg(voice={"file_id": "abc", "duration": 2}))
    assert brain == []
    assert "не змогла" in chat[-1][1].lower()


def test_start_switches_lin_to_phone(chat, brain, env):
    from raphael import runtime, tg_lin
    tg_lin.handle(msg("/start"))
    assert runtime.LIN_CHANNEL == "telegram" and brain == []


def test_speech_goes_to_chat_while_vlad_is_away(env, lin, monkeypatch):
    from raphael import tts
    said = []
    env.patch("LIN_CHANNEL", "telegram")
    env.patch("REMOTE_SAY", said.append)
    monkeypatch.setattr(tts, "_tts_play", lambda path: pytest.fail("вдома нікого: уголос не говорити"))
    assert tts.speak_stream(iter(["Сонячно, ", "плюс три."])) == "Сонячно, плюс три."
    assert said == ["Сонячно, плюс три."]


def test_chat_text_has_no_emotion_tag(chat, env):
    from raphael import tg_lin
    env.patch("TG_CHAT_ID", 5)
    tg_lin.say("[EMO:happy] Привіт, Владе.")
    assert chat == [(5, "Привіт, Владе.", {})]


def test_buttons_yes(chat, env, monkeypatch):
    from raphael import tg_lin
    env.patch("TG_CHAT_ID", 5)
    result = []
    t = threading.Thread(target=lambda: result.append(tg_lin.confirm("Вимкнути компʼютер?")))
    t.start()
    for _ in range(100):                      # чекаємо, поки кнопки «надішлються»
        if chat:
            break
        time.sleep(0.01)
    _, question, kw = chat[0]
    yes = kw["reply_markup"]["inline_keyboard"][0][0]
    assert question == "Вимкнути компʼютер?" and yes["text"] == "Так"
    tg_lin._on_button({"id": "1", "data": yes["callback_data"],
                       "message": {"chat": {"id": 5}, "message_id": 9, "text": question}})
    t.join(2)
    assert result == [True]


def test_no_answer_means_no(chat, env, monkeypatch):
    from raphael import tg_lin
    env.patch("TG_CHAT_ID", 5)
    monkeypatch.setattr(tg_lin, "CONFIRM_WAIT", 0.05)
    assert tg_lin.confirm("Стерти всі плани?") is False


def test_irreversible_asks_buttons_not_microphone(env, lin, monkeypatch):
    from raphael import assistant, stt
    asked = []
    env.patch("LIN_CHANNEL", "telegram")
    env.patch("REMOTE_SAY", lambda text: None)
    env.patch("REMOTE_CONFIRM", lambda q: asked.append(q) or True)
    monkeypatch.setattr(stt, "listen", lambda *a, **k: pytest.fail("з телефона мікрофон не слухаємо"))
    assert assistant._confirm("Вимкнути компʼютер? Скажи так або ні.") is True
    assert asked == ["Вимкнути компʼютер?"]


def test_voice_command_brings_lin_home(env, lin):
    from raphael import assistant, runtime
    env.patch("LIN_CHANNEL", "telegram")
    assistant.process_command("повтори")
    assert runtime.LIN_CHANNEL == "voice"


def test_no_vibes_to_phone(env, lin):
    from raphael import vibes
    env.patch("VIBE_ENABLED", True)
    assert vibes._can_talk() is True
    env.patch("LIN_CHANNEL", "telegram")
    assert vibes._can_talk() is False


def test_bot_does_not_start_without_allowed_people(env, lin, monkeypatch):
    from raphael import tg, tg_lin
    env.patch("TELEGRAM_BOT_TOKEN", "123:abc")
    env.patch("TELEGRAM_ALLOWED_IDS", [])
    monkeypatch.setattr(tg, "api", lambda *a, **k: pytest.fail("без списку дозволених бот не стартує"))
    tg_lin.run()
