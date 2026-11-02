"""Настрій відповіді: тег [EMO:…] не звучить, не стає дією й доходить до тіла."""
import pytest


def test_take_finds_emotion_and_strips_tag():
    from raphael import emotion
    assert emotion.take("[EMO:angry] Ну все, досить.") == ("angry", "Ну все, досить.")
    assert emotion.take("[emo: Happy]Привіт") == ("happy", "Привіт")
    assert emotion.take("[EMO:furious] Що?") == (None, "Що?")          # невідома: вирізається, не діє
    assert emotion.take("Просто текст.") == (None, "Просто текст.")


def test_emotion_tag_is_not_an_action(env):
    from raphael import llm
    assert llm._find_action("[EMO:happy] Привіт, Владе!")[0] is None
    assert llm._find_action("[EMO:happy] Відкриваю [ACTION:open_app:chrome]")[:2] == ("open_app", "chrome")
    assert llm._find_action("[EMO:sly] note_remind:мама|10")[:2] == ("note_remind", "мама|10")


def test_local_reply_with_emotion_is_fine(env):
    import types
    from raphael import llm

    def reply(text):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text, tool_calls=None))])

    llm._check_local_reply(reply("[EMO:happy] Привіт!"))                  # не кидає
    with pytest.raises(llm.BadLocalReply):
        llm._check_local_reply(reply("[EMO:happy]"))                      # самий настрій без слів


def test_prompt_asks_for_emotion_when_body_is_on(env):
    env.patch("AVATAR_ENABLED", True)
    env.patch("_brain_notes", lambda: [])
    assert "[EMO:назва]" in env.get("build_system_prompt")()
    env.patch("AVATAR_ENABLED", False)
    assert "[EMO:" not in env.get("build_system_prompt")()


def test_speech_carries_emotion_to_body_and_not_to_voice(env, tmp_path, monkeypatch):
    from raphael import avatar
    sent, synth = [], []
    env.patch("AVATAR_ENABLED", True)
    monkeypatch.setattr(avatar, "send", lambda path, data: sent.append((path, data)))
    env.patch("speak", env._saved[0][2])
    env.patch("TTS_CACHE_DIR", str(tmp_path / "tts"))
    env.patch("LIN_UI", None)
    env.patch("_tts_synth_to", lambda path, text: synth.append(text) or open(path, "wb").write(b"x"))
    env.patch("_tts_play", lambda path: None)
    env.patch("_tts_last_end", 0.0)                # інакше наступні тести мікрофона чекають луну
    env.get("speak_stream")(["[EMO:angry] Ну все, досить, я серйозно кажу. Друге речення теж довге, щоб окремо."])
    moods = [(d["text"], d.get("emo")) for p, d in sent if p == "/mind" and d["state"] == "speaking"]
    assert moods == [("Ну все, досить, я серйозно кажу.", "angry"), ("Друге речення теж довге, щоб окремо.", "angry")]
    assert not any("EMO" in t for t in synth)
