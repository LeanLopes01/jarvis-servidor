"""The PC link end to end: agent -> internal API -> hub -> WebSocket -> helper."""

import asyncio
from typing import Any

import pytest
from aiohttp import web

from src.desktop_link import DesktopLink, DesktopLinkError
from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.sessions import SessionManager
from src.jarvis_control.store import Store
from src.jarvis_control.web import build_internal_app, build_public_app
from src.jarvis_desktop.actions import ActionError, spotify_actions
from src.jarvis_desktop.client import DesktopClient, websocket_url
from src.spotify import RemoteSpotify
from src.spotify_cli import SpotifyError

INTERNAL_TOKEN = "internal-test-token"


class FakeSpotifyCli:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def play_search(
        self, query: str, kind: str, target: str = "computer"
    ) -> dict[str, Any] | None:
        self.calls.append(("play", query, target))
        if query == "nada":
            return None
        return {"name": query.title(), "artists": ["Artist"]}

    async def control(self, action: str, target: str | None = None) -> None:
        self.calls.append(("control", action))

    async def set_volume(self, level: float, target: str | None = None) -> None:
        self.calls.append(("volume", level))

    async def status(self) -> dict[str, Any]:
        raise SpotifyError("Spotify is open but not logged in.")


async def _start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


@pytest.fixture
async def link_chain():
    store = Store(":memory:")
    hub = DesktopHub(store)

    async def unit_state() -> str:
        return "inactive"

    public = build_public_app(
        store=store,
        manager=SessionManager(store, None, None),  # type: ignore[arg-type]
        hub=hub,
        rooms=None,  # type: ignore[arg-type]
        unit_state=unit_state,
        maintenance=lambda: False,
        version="test",
    )
    public_runner, public_port = await _start(public)
    internal_runner, internal_port = await _start(
        build_internal_app(hub=hub, token=INTERNAL_TOKEN)
    )

    device, token = store.redeem_pairing_code(
        store.create_pairing_code("desktop", 600, 1_000), "PC de teste", 1_001
    ) or (None, "")
    assert device is not None
    spotify = FakeSpotifyCli()
    client = DesktopClient(
        f"http://127.0.0.1:{public_port}",
        token,
        spotify_actions(spotify),  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(client.run_forever(stop))
    for _ in range(100):
        if hub.status()["online"] and hub.status()["capabilities"]:
            break
        await asyncio.sleep(0.02)

    link = DesktopLink(f"http://127.0.0.1:{internal_port}", INTERNAL_TOKEN)
    yield link, hub, spotify, stop
    stop.set()
    await asyncio.wait_for(task, 5)
    await link.aclose()
    await internal_runner.cleanup()
    await public_runner.cleanup()


def test_websocket_url() -> None:
    assert websocket_url("https://api.example/") == "wss://api.example/v1/desktop/ws"
    assert websocket_url("http://127.0.0.1:8090") == "ws://127.0.0.1:8090/v1/desktop/ws"


async def test_remote_spotify_round_trip(link_chain) -> None:
    link, _, spotify, _ = link_chain
    status = await link.status()
    assert status["online"] and "spotify.play" in status["capabilities"]
    assert status["device"] == "PC de teste"

    remote = RemoteSpotify(link)
    item = await remote.play_search("minha playlist", "playlist", "phone")
    assert item == {"name": "Minha Playlist", "artists": ["Artist"]}
    assert await remote.play_search("nada", "track") is None
    await remote.control("pause")
    await remote.set_volume(0.4)
    assert spotify.calls == [
        ("play", "minha playlist", "phone"),
        ("play", "nada", "computer"),
        ("control", "pause"),
        ("volume", 0.4),
    ]


async def test_errors_reach_the_agent(link_chain) -> None:
    link, *_ = link_chain
    with pytest.raises(DesktopLinkError, match="not logged in"):
        await link.call("spotify.status", {})
    with pytest.raises(DesktopLinkError, match="Invalid Spotify control"):
        await link.call("spotify.control", {"action": "rm -rf"})
    with pytest.raises(DesktopLinkError, match="not connected"):
        await link.call("coding.task.start", {})  # nobody offers it

    with pytest.raises(SpotifyError):
        await RemoteSpotify(link).control("format-disk")


async def test_offline_pc_is_reported(link_chain) -> None:
    link, hub, _, stop = link_chain
    stop.set()
    for _ in range(100):
        if not hub.status()["online"]:
            break
        await asyncio.sleep(0.02)
    assert (await link.status())["online"] is False
    with pytest.raises(DesktopLinkError, match="not connected"):
        await link.call("spotify.control", {"action": "pause"})


async def test_wrong_internal_token_is_rejected(link_chain) -> None:
    link, *_ = link_chain
    intruder = DesktopLink(link._base_url, "wrong")
    with pytest.raises(DesktopLinkError):
        await intruder.status()
    await intruder.aclose()


async def test_repeated_action_id_runs_once() -> None:
    runs = []

    async def count(params: dict[str, Any]) -> dict[str, Any]:
        runs.append(params)
        return {"n": len(runs)}

    async def fail(params: dict[str, Any]) -> dict[str, Any]:
        raise ActionError("nope")

    client = DesktopClient("https://example", "token", {"count": count, "fail": fail})
    first = await client.run_action({"id": "a1", "action": "count", "params": {}})
    again = await client.run_action({"id": "a1", "action": "count", "params": {}})
    assert (
        first == again == {"type": "result", "id": "a1", "ok": True, "result": {"n": 1}}
    )
    assert len(runs) == 1

    failed = await client.run_action({"id": "a2", "action": "fail"})
    assert failed["ok"] is False and failed["error"] == "nope"
    unknown = await client.run_action({"id": "a3", "action": "shell.exec"})
    assert unknown["ok"] is False
