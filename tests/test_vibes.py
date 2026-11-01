"""Характер з файлу і вайби: детект гри, ліміт реплік, контекст у промпті."""
import pytest


@pytest.fixture
def vibes(env):
    from raphael import vibes as module
    saved = {k: (list(v) if isinstance(v, list) else v) for k, v in module._vibe.items()}
    module._vibe.update(game=None, game_since=0.0, game_last_quip=0.0,
                         track=None, artist=None, music_last_quip=0.0, said=[])
    env.patch("VIBE_ENABLED", True)
    env.patch("FOCUS_ACTIVE", False)
    yield module
    module._vibe.update(saved)


# ── Характер ──────────────────────────────────────────────────────────────────

def test_persona_from_file_skips_comments(env, tmp_path):
    from raphael import prompt
    p = tmp_path / "persona.txt"
    p.write_text("# коментар\nТи Лін.\n\n  Зухвала.\n", encoding="utf-8")
    env.patch("PERSONA_FILE", str(p))
    assert prompt.persona_block() == "Ти Лін. Зухвала."
    assert prompt.build_system_prompt().startswith("Ти Лін. Зухвала. ГОЛОВНЕ")


def test_persona_missing_file_is_default(env, tmp_path):
    from raphael import prompt
    env.patch("PERSONA_FILE", str(tmp_path / "нема.txt"))
    assert prompt.persona_block() == prompt.DEFAULT_PERSONA


def test_example_persona_is_valid():
    from raphael import prompt, settings
    import os
    path = os.path.join(settings._ROOT, "persona.example.txt")
    with open(path, encoding="utf-8") as f:
        lines = [x for x in f if x.strip() and not x.startswith("#")]
    assert lines and "Рафаель" in lines[0]
    assert prompt.DEFAULT_PERSONA.startswith("Ти Рафаель")


# ── Детект гри ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,exe,expected", [
    ("Cyberpunk2077.exe", r"D:\Games\Cyberpunk2077.exe", "Cyberpunk 2077"),
    ("ArkAscended.exe", r"C:\Steam\steamapps\common\ARK Survival Ascended\ShooterGame\ArkAscended.exe",
     "ARK Survival Ascended"),
    ("game.exe", r"C:\Program Files\Epic Games\Fortnite\game.exe", "Fortnite"),
    # Wallpaper Engine працює завжди і грою не є
    ("wallpaper64.exe", r"C:\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe", None),
    ("unins000.exe", r"C:\Steam\steamapps\common\Some Game\unins000.exe", None),
    ("chrome.exe", r"C:\Program Files\Google\Chrome\chrome.exe", None),
    ("", "", None),
])
def test_game_from_process(vibes, name, exe, expected):
    assert vibes.game_from_process(name, exe) == expected


# ── Репліки ───────────────────────────────────────────────────────────────────

def test_game_start_and_long_session_end_get_a_line(vibes, env):
    lines = []
    env.patch("_line", lambda situation: lines.append(situation) or "Ну давай")
    vibes.game_tick("ARK", now=1000.0)
    assert vibes._vibe["game"] == "ARK" and len(lines) == 1
    assert "ARK" in vibes.context()
    vibes.game_tick("", now=1000.0 + 66 * 60)
    assert vibes._vibe["game"] is None and len(lines) == 2
    assert "66" in lines[1]
    assert env.spoken == ["Ну давай", "Ну давай"]


def test_short_session_end_is_silent(vibes, env):
    env.patch("_line", lambda situation: "x")
    vibes.game_tick("ARK", now=0.0)
    vibes.game_tick("", now=5 * 60)
    assert len(env.spoken) == 1


def test_hourly_limit_and_vibe_off(vibes, env):
    env.patch("_line", lambda situation: "x")
    env.patch("VIBE_MAX_PER_HOUR", 2)
    for i, game in enumerate(["A", "B", "C", "D"]):
        vibes.game_tick(game, now=float(i))
    assert len(env.spoken) == 2
    env.patch("VIBE_MAX_PER_HOUR", 10)
    vibes.toggle(False)
    vibes.game_tick("E", now=10.0)
    assert len(env.spoken) == 2


def test_silent_while_speaking(vibes, env):
    from raphael import tts
    env.patch("_line", lambda situation: "x")
    tts._tts_active.set()
    try:
        vibes.game_tick("ARK", now=0.0)
    finally:
        tts._tts_active.clear()
    assert env.spoken == []


def test_vibe_actions(lin, env, vibes):
    act = env.get("execute_action")
    act("[ACTION:vibe_off:]")
    assert env.get("VIBE_ENABLED") is False
    act("[ACTION:vibe_on:]")
    assert env.get("VIBE_ENABLED") is True
