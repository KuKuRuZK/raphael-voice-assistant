"""Міст до тіла (аватара): режим гри з монітором, де стоїть вікно гри."""
import pytest


@pytest.fixture
def avatar(env):
    from raphael import avatar as module
    saved = dict(module._last)
    module._last.update(mode=None, game=None, rect=None, online=None)
    sent = []
    env.patch("AVATAR_ENABLED", True)
    saved_post, saved_json = module.post, module.post_json
    module.post_json = lambda path, data, timeout=0.5: sent.append((path, data)) or {}
    module.post = lambda path, data, timeout=0.5: module.post_json(path, data, timeout) is not None
    module.sent = sent
    yield module
    module.post, module.post_json = saved_post, saved_json
    module._last.update(saved)


def test_game_sends_mode_and_window(avatar, monkeypatch):
    monkeypatch.setattr(avatar, "window_rect", lambda pids: [0, 0, 2560, 1440])
    data = avatar.tick("ARK Survival Ascended", [101, 102])
    assert data == {"mode": "game", "game": "ARK Survival Ascended", "rect": [0, 0, 2560, 1440]}
    assert avatar.sent == [("/state", data)]


def test_minimized_game_keeps_last_monitor(avatar, monkeypatch):
    rects = iter([[-1920, 355, 1920, 1080], None])
    monkeypatch.setattr(avatar, "window_rect", lambda pids: next(rects))
    avatar.tick("Test", [7])
    data = avatar.tick("Test", [7])                     # alt-tab: вікна не видно
    assert data["rect"] == [-1920, 355, 1920, 1080]


def test_no_game_is_normal(avatar):
    data = avatar.tick(None, [])
    assert data == {"mode": "normal", "game": None, "rect": None}


def test_post_disabled_does_nothing(env):
    from raphael import avatar
    env.patch("AVATAR_ENABLED", False)
    assert avatar.post("/state", {"mode": "normal"}) is False


def test_post_without_body_is_quick_false(env):
    import time
    from raphael import avatar
    env.patch("AVATAR_ENABLED", True)
    env.patch("AVATAR_URL", "http://127.0.0.1:9")      # там нікого
    t = time.time()
    assert avatar.post("/state", {"mode": "normal"}, timeout=0.3) is False
    assert time.time() - t < 2


def test_find_game_collects_all_processes_of_one_game(monkeypatch):
    from raphael import avatar

    class P:
        def __init__(self, pid, name, exe):
            self.info = {"pid": pid, "name": name, "exe": exe}

    procs = [P(1, "explorer.exe", r"C:\Windows\explorer.exe"),
             P(2, "launcher.exe", r"D:\Steam\steamapps\common\ARK\launcher.exe"),
             P(3, "ArkAscended.exe", r"D:\Steam\steamapps\common\ARK\Bin\ArkAscended.exe")]
    monkeypatch.setattr(avatar.psutil, "process_iter", lambda attrs: iter(procs))
    assert avatar.find_game() == ("ARK", [2, 3])


# ── Думка, музика й тіло ──────────────────────────────────────────────────────
@pytest.fixture
def sent(env, monkeypatch):
    """Що пішло тілу: send() без черги й мережі."""
    from raphael import avatar as module
    out = []
    env.patch("AVATAR_ENABLED", True)
    monkeypatch.setattr(module, "send", lambda path, data: out.append((path, data)))
    return out


def test_mind_states_go_to_body(sent):
    from raphael import avatar
    avatar.mind("listening")
    avatar.mind("thinking", "яка завтра погода")
    avatar.mind("idle")
    assert sent == [("/mind", {"state": "listening", "text": ""}),
                    ("/mind", {"state": "thinking", "text": "яка завтра погода"}),
                    ("/mind", {"state": "idle", "text": ""})]


def test_speaking_carries_loudness_for_the_mouth(sent, monkeypatch):
    from raphael import avatar
    monkeypatch.setattr(avatar, "levels_of", lambda path: [0.1, 0.9, 0.4])
    avatar.mind("speaking", "Привіт, Владе.", path="фраза.mp3")
    assert sent == [("/mind", {"state": "speaking", "text": "Привіт, Владе.", "levels": [0.1, 0.9, 0.4]})]


def test_body_does_only_what_it_can(sent):
    from raphael import avatar
    assert avatar.body("dance") is True
    assert avatar.body("format_c") is False
    assert sent == [("/do", {"action": "dance"})]


def test_avatar_action_tag_moves_the_body(sent, env):
    env.get("execute_action")("[ACTION:avatar:sitA]")
    assert sent == [("/do", {"action": "sitA"})]


def test_music_vibe(sent):
    from raphael import avatar
    avatar.vibe(True, "Трек", "Гурт")
    avatar.vibe(False)
    assert sent == [("/vibe", {"music": True, "track": "Трек", "artist": "Гурт"}),
                    ("/vibe", {"music": False, "track": None, "artist": None})]


def test_every_spoken_sentence_reaches_the_body(env, sent, tmp_path):
    """Справжній speak_stream: перед кожним реченням тіло отримує його текст."""
    real_speak = env._saved[0][2]
    env.patch("speak", real_speak)
    env.patch("TTS_CACHE_DIR", str(tmp_path / "tts"))
    env.patch("LIN_UI", None)
    env.patch("_tts_synth_to", lambda path, text: open(path, "wb").write(b"x"))
    env.patch("_tts_play", lambda path: None)
    env.patch("_tts_last_end", 0.0)                # інакше наступні тести мікрофона чекають луну
    env.get("speak_stream")(["Перше речення, досить довге, щоб не склеїтись. Друге речення теж досить довге, ось."])
    speaking = [d["text"] for p, d in sent if p == "/mind" and d["state"] == "speaking"]
    assert speaking == ["Перше речення, досить довге, щоб не склеїтись.", "Друге речення теж досить довге, ось."]


def test_queue_never_blocks_when_body_hangs(env, monkeypatch):
    from raphael import avatar
    env.patch("AVATAR_ENABLED", True)
    monkeypatch.setattr(avatar, "_sender", object())            # потік «є», але нічого не забирає
    monkeypatch.setattr(avatar, "_queue", avatar.queue.Queue(maxsize=3))
    for i in range(10):
        avatar.send("/mind", {"state": "idle", "n": i})         # не зависає на повній черзі
    left = [avatar._queue.get_nowait()[1]["n"] for _ in range(3)]
    assert left == [7, 8, 9]                                   # лишились найсвіжіші
