"""«Лайкни»: трек, що грає, у «Улюблені» Spotify і назад."""
import types

import pytest

from raphael import voice_rules as vr


class FakeSpotify:
    def __init__(self, scope, item, liked=False):
        self.liked = liked
        self.item = item
        self.calls = []
        token = {"scope": scope}
        self.auth_manager = types.SimpleNamespace(
            cache_handler=types.SimpleNamespace(get_cached_token=lambda: token))

    def current_playback(self):
        return {"item": self.item} if self.item else None

    def current_user_saved_tracks_contains(self, ids):
        return [self.liked]

    def current_user_saved_tracks_add(self, ids):
        self.calls.append(("add", ids))

    def current_user_saved_tracks_delete(self, ids):
        self.calls.append(("delete", ids))


FULL = "user-read-playback-state user-library-read user-library-modify"
TRACK = {"type": "track", "id": "t1", "name": "Stefania"}


@pytest.fixture
def spotify(env):
    def install(scope=FULL, item=TRACK, liked=False):
        sp = FakeSpotify(scope, item, liked)
        env.patch("_get_spotipy", lambda: sp)
        return sp
    return install


def test_like_current_track(env, spotify):
    sp = spotify()
    env.get("_spotify_save_current")(True)
    assert sp.calls == [("add", ["t1"])]
    assert env.spoken == ["Лайкнула «Stefania»."]


def test_old_token_asks_for_permission_once(env, spotify):
    sp = spotify(scope="user-read-playback-state user-library-read")
    env.get("_spotify_save_current")(True)
    assert sp.calls == []
    assert "spotify_auth.bat" in env.spoken[0]


@pytest.mark.parametrize("like,liked,call,said", [
    (True, True, None, "«Stefania» вже в улюблених."),
    (False, True, "delete", "Прибрала «Stefania» з улюблених."),
    (False, False, None, "«Stefania» і так не в улюблених."),
])
def test_like_is_honest_about_state(env, spotify, like, liked, call, said):
    sp = spotify(liked=liked)
    env.get("_spotify_save_current")(like)
    assert [c for c, _ in sp.calls] == ([call] if call else [])
    assert env.spoken == [said]


@pytest.mark.parametrize("item,said", [
    (None, "Зараз нічого не грає."),
    ({"type": "track", "id": None, "is_local": True, "name": "mix.mp3"}, "Це локальний файл"),
    ({"type": "episode", "id": "e1", "name": "Подкаст"}, "Це не трек"),
])
def test_like_needs_a_real_track(env, spotify, item, said):
    sp = spotify(item=item)
    env.get("_spotify_save_current")(True)
    assert sp.calls == [] and env.spoken[0].startswith(said)


def test_new_scope_is_optional():
    from raphael import spotify_common as sc
    old_token = {"scope": sc.SCOPES}                   # токен до лайків
    assert sc.token_has_scopes(old_token)              # музика й далі працює
    assert "user-library-modify" in sc.AUTH_SCOPES.split()


@pytest.mark.parametrize("text,action", [("лайкни цей трек", "spotify_like"), ("Лайк!", "spotify_like"),
                                         ("додай в улюблені", "spotify_like"),
                                         ("прибери лайк", "spotify_unlike")])
def test_like_phrases(text, action):
    assert vr.instant_command(text) == action
