"""The master phone: durable agent events, the event stream, reports, music policy
and message metadata (PLANO_CELULAR_MESTRE_JARVIS.md)."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from src import spotify_cli
from src.desktop_link import DesktopLink, DesktopLinkError
from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.events import EventError, validate_event
from src.jarvis_control.sessions import SessionManager
from src.jarvis_control.store import Store
from src.jarvis_control.web import (
    apply_music_policy,
    build_internal_app,
    build_public_app,
    session_context,
)
from src.jarvis_desktop import agent_hook
from src.jarvis_desktop.actions import spotify_actions
from src.jarvis_desktop.client import DesktopClient
from src.jarvis_desktop.outbox import Outbox, build_event
from src.spotify import SpotifyTools
from tests.test_spotify_cli import PC, PHONE, FakeCli

NOW = 2_000_000_000.0


def pair(store: Store, kind: str = "app", *, master: bool = False) -> tuple[str, str]:
    code = store.create_pairing_code(kind, 600, NOW - 10, master=master)
    redeemed = store.redeem_pairing_code(code, kind, NOW - 5)
    assert redeemed is not None
    device, token = redeemed
    return device.id, token


def hook(agent: str, data: dict[str, Any], tmp: Path) -> None:
    """What agent_hook.main does for one hook call, with test folders."""
    target = agent_hook.record(agent, data, tmp / "events")
    assert target is not None
    event = json.loads(target.read_text(encoding="utf-8"))
    agent_hook.queue_event(agent, data, event, tmp / "outbox")


# Hook and outbox


def test_every_stop_is_queued_even_quick_ones(tmp_path: Path) -> None:
    base = {"session_id": "s1", "cwd": "C:\\work\\jarvis"}
    hook("claude", {**base, "hook_event_name": "UserPromptSubmit"}, tmp_path)
    hook(
        "claude",
        {**base, "hook_event_name": "Stop", "last_assistant_message": "A"},
        tmp_path,
    )
    # A second quick turn right after: the per-session file is overwritten, the
    # outbox keeps both.
    hook("claude", {**base, "hook_event_name": "UserPromptSubmit"}, tmp_path)
    hook(
        "claude",
        {**base, "hook_event_name": "Stop", "last_assistant_message": "B"},
        tmp_path,
    )
    hook("claude", {**base, "hook_event_name": "Notification"}, tmp_path)
    hook(
        "claude",
        {
            **base,
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
        },
        tmp_path,
    )
    entries = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((tmp_path / "outbox").glob("*.json"))
    ]
    assert [(e["kind"], e["status"], e["message"]) for e in entries] == [
        ("agent_finished", "turn_completed", "A"),
        ("agent_finished", "turn_completed", "B"),
        ("agent_waiting", "question", None),
    ]
    assert len({e["event_id"] for e in entries}) == 3
    assert entries[0]["started_at"] is not None  # from UserPromptSubmit


def test_codex_turn_id_gives_a_stable_event_id(tmp_path: Path) -> None:
    data = {
        "session_id": "c1",
        "turn_id": "t-9",
        "hook_event_name": "Stop",
        "cwd": "/w/site",
    }
    hook("codex", data, tmp_path)
    hook("codex", data, tmp_path)  # the same hook delivered twice
    ids = {
        json.loads(p.read_text(encoding="utf-8"))["event_id"]
        for p in (tmp_path / "outbox").glob("*.json")
    }
    assert len(ids) == 1


def test_host_hint() -> None:
    assert agent_hook.host_hint(
        "claude", {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}
    ) == ("claude-vscode")
    assert agent_hook.host_hint("codex", {"TERM_PROGRAM": "vscode"}) == "vscode"
    assert agent_hook.host_hint("codex", {}) == "unknown"


def test_event_is_completed_from_the_transcript_and_redacted(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    long_answer = "Refactored the login flow. All 42 tests pass. " * 30
    records = [
        {
            "type": "user",
            "cwd": "C:\\work\\jarvis",
            "entrypoint": "claude-vscode",
            "message": {
                "role": "user",
                "content": "Fix the login, token: abc12345secret",
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "C:\\work\\jarvis\\login.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": long_answer + "password: hunter22"}
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    entry = {
        "event_id": "e" * 32,
        "kind": "agent_finished",
        "status": "turn_completed",
        "agent": "claude",
        "host": "unknown",
        "session_id": "s1",
        "cwd": "C:\\work\\jarvis",
        "transcript_path": str(transcript),
        "message": None,  # this Claude version sent no final message
        "finished_at": NOW,
    }
    event = build_event(entry, NOW)
    assert event["title"] == "Claude Code finished · jarvis"
    assert event["host"] == "VS Code"
    assert event["data"]["files_changed"] == ["login.py"]
    assert "hunter22" not in event["report"] and "abc12345secret" not in str(event)
    assert len(event["summary"]) <= 300 < len(event["report"])
    # The server accepts what the PC builds.
    validate_event(event)


def test_outbox_resends_until_acknowledged(tmp_path: Path) -> None:
    hook(
        "claude",
        {"session_id": "s", "hook_event_name": "Stop", "cwd": "/w/a"},
        tmp_path,
    )
    outbox = Outbox(tmp_path / "outbox")
    now = time.time()
    first = outbox.due(now)
    assert len(first) == 1 and first[0]["type"] == "agent_event"
    assert outbox.due(now + 5) == []  # in flight
    again = outbox.due(now + 31)  # no answer: send the very same event again
    assert again[0]["event"] == first[0]["event"]
    assert outbox.status()["pending"] == 1
    assert outbox.acknowledge({"event_id": first[0]["event"]["event_id"], "ok": True})
    assert outbox.status()["pending"] == 0


def test_server_rejects_malformed_events() -> None:
    with pytest.raises(EventError):
        validate_event({"event_id": "short", "kind": "agent_finished", "title": "x"})
    with pytest.raises(EventError):
        validate_event({"event_id": "a" * 20, "kind": "rm -rf", "title": "x"})
    with pytest.raises(EventError):
        validate_event({"event_id": "a" * 20, "kind": "agent_finished"})


# Store


def test_master_role_is_unique_and_set_by_pairing() -> None:
    store = Store(":memory:")
    first, _ = pair(store, master=True)
    assert (
        store.profile(first).master and store.profile(first).music_target == "computer"
    )
    second, _ = pair(store)
    assert not store.profile(second).master
    assert store.set_master(second, True, NOW)
    assert store.profile(second).master and not store.profile(first).master
    desktop, _ = pair(store, "desktop")
    assert not store.set_master(desktop, True, NOW)
    with pytest.raises(ValueError):
        store.create_pairing_code("desktop", 60, NOW, master=True)


def test_session_context_for_a_master_command() -> None:
    store = Store(":memory:")
    master, _ = pair(store, master=True)
    context = session_context({"command": {"text": " pause the music "}}, store, master)
    assert context == {
        "profile": {"master": True, "music_target": "computer"},
        "command": {"text": "pause the music"},
        "audio_input": False,
    }
    other, _ = pair(store)
    assert session_context({}, store, other) is None
    assert session_context({"audio": False}, store, other) == {"audio_input": False}


# Server with a connected PC and the master phone


class FakeSpotify:
    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def play_search(self, query: str, kind: str, target: str = "computer"):
        self.calls.append(("play", query, target))
        return {
            "name": query,
            "artists": [],
            "device": "BATCOMPUTADOR",
            "confirmed": True,
        }

    async def control(self, action: str, target: str | None = None):
        self.calls.append(("control", action, target))
        return {"confirmed": True}

    async def set_volume(self, level: float, target: str | None = None):
        self.calls.append(("volume", level, target))
        return {}

    async def playback_state(self):
        return {
            "track": "Song — A",
            "is_playing": True,
            "device": "BATCOMPUTADOR",
            "device_id": "pc1",
            "on_computer": True,
        }

    async def status(self):
        return {"running": True, "logged_in": True}


async def _start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


@pytest.fixture
async def master_chain(tmp_path: Path):
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
    runner, port = await _start(public)
    master_id, master_token = pair(store, master=True)
    _, other_token = pair(store)
    _, desktop_token = pair(store, "desktop")
    spotify = FakeSpotify()
    outbox = Outbox(tmp_path / "outbox")
    client = DesktopClient(
        f"http://127.0.0.1:{port}",
        desktop_token,
        spotify_actions(spotify),  # type: ignore[arg-type]
        outbox=outbox,
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(client.run_forever(stop))
    for _ in range(200):
        if hub.status()["capabilities"]:
            break
        await asyncio.sleep(0.02)
    yield {
        "store": store,
        "hub": hub,
        "url": f"http://127.0.0.1:{port}",
        "master": {"Authorization": f"Bearer {master_token}"},
        "other": {"Authorization": f"Bearer {other_token}"},
        "master_id": master_id,
        "spotify": spotify,
        "tmp": tmp_path,
        "stop": stop,
    }
    stop.set()
    await asyncio.wait_for(task, 5)
    await runner.cleanup()


async def _wait(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_finished_task_reaches_the_master_stream(master_chain) -> None:
    import aiohttp

    chain = master_chain
    async with aiohttp.ClientSession() as http:
        # Other phones are not linked to the reports.
        denied = await http.get(
            f"{chain['url']}/v1/events?after=0", headers=chain["other"]
        )
        assert denied.status == 403

        ws = await http.ws_connect(
            f"{chain['url']}/v1/events/stream?after=0", headers=chain["master"]
        )
        ready = await ws.receive_json(timeout=5)
        assert ready == {"type": "ready", "cursor": "0"}

        hook(
            "codex",
            {
                "session_id": "c1",
                "turn_id": "t1",
                "hook_event_name": "Stop",
                "cwd": "/w/site",
                "last_assistant_message": "Deployed the site.",
            },
            chain["tmp"],
        )
        message = await ws.receive_json(timeout=5)
        event = message["event"]
        assert message["type"] == "event"
        assert event["title"] == "Codex finished · site"
        assert event["has_report"] and "report" not in event
        # Deleted on the PC only after the server stored it.
        await _wait(lambda: not list((chain["tmp"] / "outbox").glob("*.json")))

        await ws.send_json(
            {"type": "ack", "event_id": event["id"], "stage": "received"}
        )
        report = await (
            await http.get(
                f"{chain['url']}/v1/reports/{event['id']}", headers=chain["master"]
            )
        ).json()
        assert report["report"] == "Deployed the site."
        read = await http.post(
            f"{chain['url']}/v1/events/{event['id']}/ack",
            json={"stage": "read"},
            headers=chain["master"],
        )
        assert read.status == 204
        stages = chain["store"].deliveries(event["id"])[0]
        assert (
            stages["received_at"] and stages["read_at"] and not stages["announced_at"]
        )

        # Catching up after a gap: the cursor returns what the stream missed.
        listing = await (
            await http.get(f"{chain['url']}/v1/events?after=0", headers=chain["master"])
        ).json()
        assert [e["id"] for e in listing["events"]] == [event["id"]]
        assert listing["cursor"] == event["cursor"]
        await ws.close()

        diagnostics = await (
            await http.get(f"{chain['url']}/v1/diagnostics", headers=chain["master"])
        ).json()
        assert diagnostics["coverage"][0]["agent"] == "codex"
        assert diagnostics["desktop"]["online"] is True


async def test_a_repeated_event_is_stored_once(master_chain) -> None:
    store, hub = master_chain["store"], master_chain["hub"]
    event = {
        "event_id": "x" * 20,
        "kind": "agent_finished",
        "title": "Codex finished · a",
    }
    conn = next(iter(hub._connections.values()))
    first = hub._store_event(conn, event)
    second = hub._store_event(conn, event)  # the PC lost the first ack
    assert (
        first == second == {"type": "agent_event_ack", "event_id": "x" * 20, "ok": True}
    )
    assert len(store.events_after(0)) == 1
    # Older app versions still see it in the 15-minute notification check.
    assert store.notifications_after(0)[0]["title"] == "Codex finished · a"


async def test_revoked_master_stream_is_closed(master_chain, monkeypatch) -> None:
    import aiohttp

    from src.jarvis_control import web as web_module

    monkeypatch.setattr(web_module, "STREAM_RECHECK_SECONDS", 0.1)
    chain = master_chain
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(
            f"{chain['url']}/v1/events/stream", headers=chain["master"]
        )
        assert (await ws.receive_json(timeout=5))["type"] == "ready"
        chain["store"].revoke_device(chain["master_id"], time.time())
        closing = await ws.receive(timeout=5)
        assert closing.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}


async def test_master_music_commands_play_only_on_the_computer(master_chain) -> None:
    import aiohttp

    chain = master_chain
    url = f"{chain['url']}/v1/commands"
    async with aiohttp.ClientSession() as http:
        played = await http.post(
            url,
            json={
                "id": "c1",
                "kind": "music.play",
                "params": {"query": "Back in Black"},
            },
            headers=chain["master"],
        )
        assert played.status == 200
        again = await http.post(
            url,
            json={
                "id": "c1",
                "kind": "music.play",
                "params": {"query": "Back in Black"},
            },
            headers=chain["master"],
        )
        assert again.status == 200  # the stored answer, not a second play
        phone = await http.post(
            url,
            json={
                "id": "c2",
                "kind": "music.play",
                "params": {"query": "x", "device": "phone"},
            },
            headers=chain["master"],
        )
        assert phone.status == 403
        await http.post(
            url,
            json={"id": "c3", "kind": "music.control", "params": {"action": "next"}},
            headers=chain["master"],
        )
        await http.post(
            url,
            json={"id": "c4", "kind": "music.volume", "params": {"percent": 30}},
            headers=chain["master"],
        )
        stale = await http.post(
            url,
            json={
                "id": "c5",
                "kind": "music.control",
                "params": {"action": "resume"},
                "deadline": time.time() - 1,
            },
            headers=chain["master"],
        )
        assert stale.status == 410
        status = await (
            await http.post(
                url, json={"id": "c6", "kind": "music.status"}, headers=chain["master"]
            )
        ).json()
        assert status["result"]["on_computer"] is True
        # A phone that is not the master may still choose the phone.
        other = await http.post(
            url,
            json={
                "id": "c7",
                "kind": "music.play",
                "params": {"query": "y", "device": "phone"},
            },
            headers=chain["other"],
        )
        assert other.status == 200
    assert chain["spotify"].calls == [
        ("play", "Back in Black", "computer"),
        ("control", "next", "computer"),
        ("volume", 0.3, "computer"),
        ("play", "y", "phone"),
    ]


async def test_music_command_with_the_pc_offline(master_chain) -> None:
    import aiohttp

    chain = master_chain
    chain["stop"].set()
    await _wait(lambda: not chain["hub"].status()["online"])
    async with aiohttp.ClientSession() as http:
        response = await http.post(
            f"{chain['url']}/v1/commands",
            json={"id": "z1", "kind": "music.control", "params": {"action": "resume"}},
            headers=chain["master"],
        )
        assert response.status == 503
        assert (await response.json())["error"] == "pc_offline"


async def test_message_metadata_only_from_whatsapp_and_instagram(master_chain) -> None:
    import aiohttp

    chain = master_chain
    url = f"{chain['url']}/v1/mobile-notifications"
    async with aiohttp.ClientSession() as http:
        sms = await http.post(
            url, json={"items": [{"key": "k", "app": "sms"}]}, headers=chain["master"]
        )
        assert sms.status == 400
        item = {"key": "wa:1", "app": "whatsapp", "sender": "Ana", "text": "segredo"}
        stored = await (
            await http.post(url, json={"items": [item, item]}, headers=chain["master"])
        ).json()
        assert stored == {"stored": 1}
    rows = chain["store"].mobile_notifications()
    assert rows[0]["sender"] == "Ana" and "segredo" not in json.dumps(rows)


# The agent's own Spotify calls during a master-phone session


def test_internal_policy_forces_the_computer() -> None:
    assert apply_music_policy("spotify.control", {"action": "next"}, "computer") == {
        "action": "next",
        "device": "computer",
    }
    with pytest.raises(PermissionError):
        apply_music_policy(
            "spotify.play", {"query": "x", "device": "phone"}, "computer"
        )
    assert apply_music_policy("spotify.play", {"device": "phone"}, None) == {
        "device": "phone"
    }
    assert apply_music_policy("browser.open", {"what": "x"}, "computer") == {
        "what": "x"
    }


async def test_internal_api_refuses_the_phone_in_a_master_session(master_chain) -> None:
    runner, port = await _start(
        build_internal_app(
            hub=master_chain["hub"], token="t", music_target=lambda: "computer"
        )
    )
    link = DesktopLink(f"http://127.0.0.1:{port}", "t")
    try:
        with pytest.raises(DesktopLinkError, match="only on the computer"):
            await link.call(
                "spotify.play", {"query": "x", "kind": "track", "device": "phone"}
            )
        await link.call("spotify.control", {"action": "pause"})
    finally:
        await link.aclose()
        await runner.cleanup()
    assert master_chain["spotify"].calls == [("control", "pause", "computer")]


def test_locked_spotify_tools_have_no_device_choice() -> None:
    locked = SpotifyTools(FakeSpotify(), locked_target="computer")  # type: ignore[arg-type]
    free = SpotifyTools(FakeSpotify())  # type: ignore[arg-type]
    import inspect

    locked_play, free_play = locked.tools[0], free.tools[0]
    assert locked_play.info.name == free_play.info.name == "play_on_spotify"
    assert "device" not in inspect.signature(locked_play).parameters
    assert "device" in inspect.signature(free_play).parameters


async def test_locked_spotify_tool_plays_on_the_computer() -> None:
    spotify = FakeSpotify()
    tools = SpotifyTools(spotify, locked_target="computer")  # type: ignore[arg-type]
    reply = await tools.play_on_computer(None, "Back in Black")  # type: ignore[arg-type]
    await tools.control_spotify(None, "previous")  # type: ignore[arg-type]
    assert "BATCOMPUTADOR" in reply
    assert spotify.calls == [
        ("play", "Back in Black", "computer"),
        ("control", "previous", "computer"),
    ]


# Spotify on the PC


@pytest.fixture
def instant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify_cli, "TRANSFER_SETTLE_SECONDS", 0)
    monkeypatch.setattr(spotify_cli, "CONFIRM_SECONDS", 0)


async def test_next_brings_playback_back_to_the_computer(instant) -> None:
    cli = FakeCli([{**PC, "is_active": False}, {**PHONE, "is_active": True}])
    await cli.control("next", "computer")
    assert cli.calls[:2] == [("devices", "transfer", "pc1"), ("next",)]
    await cli.set_volume(0.5, "computer")
    assert ("volume", "0.50") in cli.calls


async def test_pause_never_moves_playback(instant) -> None:
    cli = FakeCli([{**PC, "is_active": False}, {**PHONE, "is_active": True}])
    await cli.control("pause", "computer")
    assert cli.calls == [("pause",)]


async def test_playback_state_reports_the_active_device(instant) -> None:
    cli = FakeCli([{**PC, "is_active": True}, PHONE])
    state = await cli.playback_state()
    assert state["on_computer"] is True and state["device"] == "BATCOMPUTADOR"
    item = await cli.play_search("song", "track", "computer")
    assert item is not None and item["confirmed"] is True


# Antigravity (Gemini) quota for the master phone's usage panel


def test_antigravity_live_status_is_parsed_without_personal_data(
    tmp_path: Path,
) -> None:
    from src.jarvis_desktop import antigravity

    status = {
        "userStatus": {
            "name": "Someone",
            "email": "someone@example.com",
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "Gemini Flash (High)",
                        "quotaInfo": {
                            "remainingFraction": 0.904,
                            "resetTime": "2033-05-24T10:29:00Z",
                        },
                    },
                    {
                        "label": "Gemini Pro",
                        "quotaInfo": {"resetTime": "2033-05-25T10:00:00Z"},
                    },
                    {"label": "Not on this plan", "quotaInfo": {}},
                ]
            },
        }
    }
    models = antigravity.parse_status(status, NOW)
    assert [(m["label"], m["remaining_percent"]) for m in models] == [
        ("Gemini Flash (High)", 90.4),
        ("Gemini Pro", 0.0),
    ]
    assert "someone" not in json.dumps(models)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"observed_at": NOW - 120, "models": models}))
    later = antigravity.antigravity_usage(NOW, live=False, logs=tmp_path, cache=cache)
    assert later["source"] == "last reading" and later["observed_minutes_ago"] == 2
    assert later["models"][0]["resets_in_minutes"] > 0
    after_reset = antigravity.antigravity_usage(
        NOW + 400 * 86400, live=False, logs=tmp_path, cache=cache
    )
    assert after_reset is None  # too old to show


def test_antigravity_limit_from_logs(tmp_path: Path) -> None:
    from datetime import datetime

    from src.jarvis_desktop import antigravity

    log = tmp_path / "20330519T070000" / "ls-main.log"
    log.parent.mkdir()
    log.write_text(
        "2033-05-19 07:30:25.087 [error] [LS Main stderr] E0519 RESOURCE_EXHAUSTED "
        "(code 429): You have exhausted your capacity on this model. Your quota will "
        "reset after 35h31m1s.: RESOURCE_EXHAUSTED\n",
        encoding="utf-8",
    )
    import os

    os.utime(log, (NOW, NOW))
    now = datetime(2033, 5, 19, 8, 0).timestamp()
    hit = antigravity.limit_from_logs(now, tmp_path)
    assert hit == {"resets_at": "2033-05-20 19:01", "resets_in_minutes": 2101}
    assert antigravity.limit_from_logs(now + 3 * 86400, tmp_path) is None


async def test_usage_reaches_the_phone(master_chain, monkeypatch) -> None:
    import aiohttp

    chain = master_chain
    async with aiohttp.ClientSession() as http:
        # The test PC offers no coding.usage action: the phone is told plainly.
        missing = await http.get(f"{chain['url']}/v1/usage", headers=chain["other"])
        assert missing.status == 503
        assert (await missing.json())["error"] == "pc_offline"
