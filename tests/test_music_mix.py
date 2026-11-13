"""Лін сама підбирає музику: підбірка, конкретний трек, плейлист (2026-11-13: «chill» вмикав трек «CHILL»)."""
import types

import pytest


def track(artist, name, uri=None):
    return {"artists": [{"name": artist}], "name": name, "uri": uri or f"spotify:track:{artist}-{name}"}


class FakeSpotify:
    def __init__(self, catalog, scope="user-read-playback-state"):
        self.catalog, self.played, self.searches, self.created, self.added = catalog, [], [], [], []
        self.auth_manager = types.SimpleNamespace(cache_handler=types.SimpleNamespace(
            get_cached_token=lambda: {"scope": scope}))

    def search(self, q, type, limit):
        self.searches.append(q)
        words = q.lower().replace('"', " ").replace("track:", " ").replace("artist:", " ").split()
        hits = [t for t in self.catalog if all(w in (t["artists"][0]["name"] + " " + t["name"]).lower() for w in words)]
        return {"tracks": {"items": hits[:limit]}}

    def start_playback(self, uris=None, context_uri=None):
        self.played.append(uris or context_uri)

    def current_user_recently_played(self, limit):
        return {"items": [{"track": track("Arctic Monkeys", "505")}]}

    def current_user_saved_tracks(self, limit):
        return {"items": [{"track": track("Daft Punk", "Get Lucky")}]}

    def current_user_top_artists(self, limit, time_range):
        raise RuntimeError("Insufficient client scope")

    def current_user(self):
        return {"id": "vlad"}

    def user_playlist_create(self, user, name, public, description):
        self.created.append((user, name, public, description))
        return {"id": "pl1", "uri": "spotify:playlist:pl1"}

    def playlist_add_items(self, pid, uris):
        self.added.append((pid, uris))


CATALOG = [track("Imagine Dragons", "Believer"), track("Imagine Dragons", "Believer (Live)"),
           track("Believer Kids", "Imagine"), track("Arctic Monkeys", "505"), track("Daft Punk", "Get Lucky"),
           track("Tame Impala", "The Less I Know The Better"), track("CHILL", "chill"), track("Viber", "vibe")]


@pytest.fixture
def mm(monkeypatch):
    from raphael import music, music_mix
    sp = FakeSpotify(CATALOG)
    monkeypatch.setattr(music, "_get_spotipy", lambda: sp)
    monkeypatch.setattr(music, "with_device", lambda s, call: call())
    asked = []

    def picks(answer):
        def chat(model, messages, **kw):
            asked.append(messages[0]["content"])
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=answer))])
        monkeypatch.setattr(music_mix.llm, "llm_chat", chat)
    return music_mix, sp, picks, asked


@pytest.mark.parametrize("line,pick", [
    ("1. Imagine Dragons - Believer", ("Imagine Dragons", "Believer")),
    ("- Tame Impala \u2013 The Less I Know The Better", ("Tame Impala", "The Less I Know The Better")),
    ("«Daft Punk — Get Lucky»", ("Daft Punk", "Get Lucky")),
    ("просто текст", None), ("Spider-Man", None),
])
def test_pick_lines(mm, line, pick):
    assert mm[0].split_pick(line) == pick


def test_exact_track_not_the_first_hit(mm):
    music_mix, sp, _, _ = mm
    assert music_mix.play("Imagine Dragons - Believer") == "Imagine Dragons - Believer"
    assert sp.played == [["spotify:track:Imagine Dragons-Believer"]]


def test_vague_word_is_not_played_as_a_track(mm):
    music_mix, sp, _, _ = mm
    assert music_mix.find(sp, "Нехтуй", "Неіснуюча пісня") is None


def test_mix_is_her_own_choice_with_taste(mm):
    music_mix, sp, picks, asked = mm
    picks("Tame Impala - The Less I Know The Better\nДaft Punk - Неіснуючий трек\n1. Arctic Monkeys - 505\nкоментар")
    said = music_mix.mix("щось спокійне на вечір")
    assert "щось спокійне на вечір" in asked[0] and "Arctic Monkeys - 505" in asked[0]   # смак Влада в промті
    assert sp.played == [["spotify:track:Tame Impala-The Less I Know The Better", "spotify:track:Arctic Monkeys-505"]]
    assert said == "Поставила 2 треки. Почнемо з Tame Impala - The Less I Know The Better."


def test_mix_when_nothing_found_says_so(mm):
    music_mix, sp, picks, _ = mm
    picks("Нема Такого - Трек\nІнший - Теж нема")
    assert "жодного не знайшла" in music_mix.mix("") and sp.played == []


def test_playlist_needs_permission_first(mm):
    music_mix, sp, picks, _ = mm
    assert "spotify_auth.bat" in music_mix.make_playlist("Для пробіжки|енергійне") and sp.created == []


def test_playlist_is_built_saved_and_played(mm):
    music_mix, sp, picks, _ = mm
    sp.auth_manager.cache_handler.get_cached_token = lambda: {"scope": "playlist-modify-private user-read-playback-state"}
    picks("Imagine Dragons - Believer\nDaft Punk - Get Lucky")
    said = music_mix.make_playlist("Для пробіжки|енергійне для бігу")
    assert sp.created == [("vlad", "Для пробіжки", False, "Зібрала Лін: енергійне для бігу")]
    assert sp.added == [("pl1", ["spotify:track:Imagine Dragons-Believer", "spotify:track:Daft Punk-Get Lucky"])]
    assert sp.played == ["spotify:playlist:pl1"] and "2 треки" in said


def test_old_token_still_works_without_new_permissions():
    from raphael import spotify_common
    assert spotify_common.token_has_scopes({"scope": spotify_common.SCOPES})
    assert "playlist-modify-private" in spotify_common.AUTH_SCOPES


def test_prompt_sends_vague_requests_to_mix():
    from raphael import prompt
    import inspect
    src = inspect.getsource(prompt)
    assert "spotify_mix:опис" in src and "НІКОЛИ spotify_play з одним словом" in src
