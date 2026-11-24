"""SpotifyCli device handling with a fake spotify_cli."""

import pytest

from src import spotify_cli
from src.spotify_cli import SpotifyCli, SpotifyError

PC = {
    "device_id": "pc1",
    "name": "BATCOMPUTADOR",
    "device_type": "computer",
    "is_self": True,
}
PHONE = {
    "device_id": "ph1",
    "name": "Pixel",
    "device_type": "smartphone",
    "is_self": False,
}


class FakeCli(SpotifyCli):
    def __init__(self, devices: list[dict], playing: bool = True) -> None:
        self._cli = "spotify_cli"
        self.devices_list = devices
        self.playing = playing
        self.calls: list[tuple[str, ...]] = []
        self.fail_pause = False

    async def _run(self, *args: str) -> str:
        self.calls.append(args)
        if args == ("pause",) and self.fail_pause:
            raise SpotifyError("Failed to pause: client unknown error")
        return ""

    async def _json(self, *args: str):
        if args[0] == "status":
            return {"running": True, "logged_in": True}
        if args[:2] == ("devices", "list"):
            return {"devices": self.devices_list}
        if args[0] == "search":
            return {
                "tracks": [{"uri": "spotify:track:x", "name": "Song", "artists": ["A"]}]
            }
        if args[0] == "now-playing":
            return {"currently_playing": {"is_playing": self.playing}}
        raise AssertionError(args)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify_cli, "TRANSFER_SETTLE_SECONDS", 0)
    monkeypatch.setattr(spotify_cli, "CONFIRM_SECONDS", 0)


async def test_plays_on_active_computer_without_device_flag() -> None:
    cli = FakeCli([{**PC, "is_active": True}])
    item = await cli.play_search("song", "track", "computer")
    assert item == {
        "name": "Song",
        "artists": ["A"],
        "device": "BATCOMPUTADOR",
        "confirmed": True,
    }
    plays = [c for c in cli.calls if c[0] == "play"]
    assert plays == [("play", "spotify:track:x")]  # never `play URI --device`
    assert not any(c[:2] == ("devices", "transfer") for c in cli.calls)


async def test_transfers_to_the_phone_before_playing() -> None:
    cli = FakeCli([{**PC, "is_active": True}, {**PHONE, "is_active": False}])
    item = await cli.play_search("song", "track", "phone")
    assert item is not None and item["device"] == "Pixel"
    order = [c for c in cli.calls if c[0] in {"devices", "play"}]
    assert order == [("devices", "transfer", "ph1"), ("play", "spotify:track:x")]


async def test_missing_phone_is_explained() -> None:
    cli = FakeCli([{**PC, "is_active": True}])
    with pytest.raises(SpotifyError, match="Open the Spotify app on the phone"):
        await cli.play_search("song", "track", "phone")


async def test_pause_when_already_paused_is_fine() -> None:
    cli = FakeCli([PC], playing=False)
    cli.fail_pause = True
    await cli.control("pause")
    cli.playing = True
    with pytest.raises(SpotifyError):
        await cli.control("pause")
