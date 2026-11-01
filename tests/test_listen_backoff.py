"""Без мікрофона listen() не забиває лог і не крутиться щосекунди."""
import logging


def test_missing_mic_backs_off_and_logs_once(env, lin, caplog):
    from raphael import stt
    sr = env.get("sr")
    sleeps = []

    class NoMic:
        def __enter__(self):
            raise OSError("No Default Input Device Available")

        def __exit__(self, *a):
            pass

    class Mic:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    env._saved.append((sr, "Microphone", sr.Microphone))
    sr.Microphone = NoMic
    env.patch("LIN_UI", None)
    env.patch("_listen_failures", 0)
    env._saved.append((stt.time, "sleep", stt.time.sleep))
    stt.time.sleep = sleeps.append

    with caplog.at_level(logging.DEBUG, logger="Лін"):
        for _ in range(8):
            assert stt.listen() == ""
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]

    # Мікрофон повернувся: лічильник скинуто, про це є рядок у лозі
    sr.Microphone = Mic
    recognizer = env.get("recognizer")
    env._saved.append((recognizer, "listen", recognizer.listen))
    recognizer.listen = lambda source, timeout, phrase_time_limit: "AUDIO"
    env._saved.append((recognizer, "adjust_for_ambient_noise", recognizer.adjust_for_ambient_noise))
    recognizer.adjust_for_ambient_noise = lambda source, duration: None
    env.patch("_transcribe_whisper", lambda audio: "рафа")
    with caplog.at_level(logging.INFO, logger="Лін"):
        assert stt.listen() == "рафа"
    assert stt._listen_failures == 0
    assert any("знову працює" in r.getMessage() for r in caplog.records)
