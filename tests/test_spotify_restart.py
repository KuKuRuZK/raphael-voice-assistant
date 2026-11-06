"""«Почни плейлист спочатку»: свій тег, а не spotify_play, яке на музику, що грає, нічого не робить."""
import pytest


class Sp:
    def __init__(self, state):
        self.state, self.calls = state, []

    def current_playback(self):
        return self.state

    def seek_track(self, ms):
        self.calls.append(("seek", ms))

    def shuffle(self, on):
        self.calls.append(("shuffle", on))

    def start_playback(self, **kw):
        self.calls.append(("play", kw))


@pytest.fixture
def music(env):
    from raphael import music
    return music


def test_playlist_from_first_track(music, env):
    sp = Sp({"context": {"uri": "spotify:playlist:abc"}, "shuffle_state": True})
    env.patch("_get_spotipy", lambda: sp)
    music._spotify_restart("плейлист")
    assert sp.calls == [("shuffle", False), ("play", {"context_uri": "spotify:playlist:abc", "offset": {"position": 0}})]
    assert env.spoken == ["Граю плейлист з першого треку."]


def test_track_from_start(music, env):
    sp = Sp({"context": {"uri": "spotify:album:x"}})
    env.patch("_get_spotipy", lambda: sp)
    music._spotify_restart("трек")
    assert sp.calls == [("seek", 0)]


def test_no_playlist_restarts_the_track(music, env):
    sp = Sp({"context": None})
    env.patch("_get_spotipy", lambda: sp)
    music._spotify_restart("")
    assert sp.calls == [("seek", 0)] and "трек спочатку" in env.spoken[0].lower()


def test_without_word_it_is_the_track(music, env):
    """«Включи спочатку» майже завжди про пісню: плейлист лише зі словом плейлист чи альбом."""
    sp = Sp({"context": {"uri": "spotify:playlist:abc"}})
    env.patch("_get_spotipy", lambda: sp)
    music._spotify_restart("")
    assert sp.calls == [("seek", 0)]


@pytest.mark.parametrize("text", ["а можеш почати грати цей плейліст спочатку?", "постав трек спочатку"])
def test_restart_requests_go_to_cloud(lin, env, text):
    from raphael import llm
    env.patch("BRAIN_LOCAL_MODEL", "lmstudio:google/gemma-4-26b-a4b-qat")
    env.patch("GAME", None)
    env.patch("FAST_MODEL", "")
    assert llm._choose_model(text) == env.get("GROQ_PRIMARY_MODEL")


def test_restart_tag_is_an_action(lin):
    from raphael import llm
    assert llm._find_action("[EMO:tease] Погнали з початку. [ACTION:spotify_restart:плейлист]")[:2] == \
        ("spotify_restart", "плейлист")


@pytest.mark.parametrize("what", ["трек", "тре��", "Пісню", "track"])
def test_track_word_even_when_broken(music, env, what):
    sp = Sp({"context": {"uri": "spotify:playlist:abc"}})
    env.patch("_get_spotipy", lambda: sp)
    music._spotify_restart(what)
    assert sp.calls == [("seek", 0)]
