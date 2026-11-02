"""Spotify без активного пристрою: Лін сама будить ПК замість «відкрий Spotify»."""
import pytest


class NoDevice(Exception):
    def __init__(self):
        super().__init__("http status: 404, code: -1 - Player command failed: No active device found, reason: NO_ACTIVE_DEVICE")


class FakeSpotify:
    def __init__(self, devices, fail_times=1):
        self._devices, self.fail_times = devices, fail_times
        self.transfers, self.played = [], 0

    def devices(self):
        return {"devices": self._devices}

    def transfer_playback(self, device_id, force_play=True):
        self.transfers.append((device_id, force_play))

    def start_playback(self, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise NoDevice()
        self.played += 1


@pytest.fixture
def music(env, monkeypatch):
    from raphael import music
    monkeypatch.setattr(music.time, "sleep", lambda s: None)
    return music


def test_wakes_the_pc_and_plays(music):
    sp = FakeSpotify([{"id": "phone", "type": "Smartphone", "name": "Pixel"},
                      {"id": "pc", "type": "Computer", "name": "DESKTOP"}])
    music.with_device(sp, sp.start_playback)
    assert sp.transfers == [("pc", False)] and sp.played == 1


def test_starts_the_app_when_no_devices(music, monkeypatch):
    sp = FakeSpotify([])
    started = []

    def startfile(uri):
        started.append(uri)
        sp._devices.append({"id": "pc", "type": "Computer", "name": "DESKTOP"})
    monkeypatch.setattr(music.os, "startfile", startfile)
    music.with_device(sp, sp.start_playback)
    assert started == ["spotify:"] and sp.transfers == [("pc", False)] and sp.played == 1


def test_gives_up_when_app_never_appears(music, monkeypatch):
    sp = FakeSpotify([])
    monkeypatch.setattr(music.os, "startfile", lambda uri: None)
    with pytest.raises(NoDevice):
        music.with_device(sp, sp.start_playback)


def test_other_errors_are_not_touched(music):
    sp = FakeSpotify([{"id": "pc", "type": "Computer", "name": "DESKTOP"}])

    def boom():
        raise RuntimeError("403 premium required")
    with pytest.raises(RuntimeError):
        music.with_device(sp, boom)
    assert sp.transfers == []
