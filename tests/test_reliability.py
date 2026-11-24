"""Section 13 of PLANO_ORQUESTRACAO_DICEVTT_JARVIS.md: status, real cancellation and
reports about JARVIS itself."""

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from src import spotify_cli
from src.jarvis_control.commands import CommandRunner
from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.diagnostics import Diagnostics, redact, report_name
from src.jarvis_control.notes import NotesError, NotesWriter
from src.jarvis_control.operations import Operations
from src.jarvis_control.sessions import SessionManager
from src.jarvis_control.store import Store
from src.jarvis_control.web import build_internal_app, build_public_app
from src.jarvis_desktop.actions import ActionError
from src.jarvis_desktop.client import DesktopClient
from tests.test_master_phone import _start, _wait, pair


class SlowSpotify:
    """Spotify whose resume hangs until released, like the reported freeze."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls: list[str] = []

    async def control(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params["action"])
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"confirmed": True}

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "track": "Song",
            "is_playing": True,
            "device": "BATCOMPUTADOR",
            "on_computer": True,
        }

    async def snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": "BATCOMPUTADOR",
            "log": "2026-10-18 11:00:00,000 INFO token: abcdef123456 connected",
        }

    async def broken(self, params: dict[str, Any]) -> dict[str, Any]:
        raise ActionError("nope")


class FakeWriter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.files: dict[str, bytes] = {}

    async def put(self, relative: str, data: bytes, *, overwrite: bool = False) -> int:
        if self.fail:
            raise NotesError("Nextcloud is down")
        if relative in self.files and not overwrite:
            raise NotesError(f"{relative} already exists")
        self.files[relative] = data
        return len(data)


@pytest.fixture
async def chain(tmp_path: Path):
    store = Store(":memory:")
    hub = DesktopHub(store)
    ops = Operations(store, hub.bus)
    hub.late_result = ops.action_result
    ops.cancel_action = hub.cancel_action
    commands = CommandRunner(store, hub, ops)
    writer = FakeWriter()

    async def unit_state() -> str:
        return "inactive"

    diagnostics = Diagnostics(
        store,
        hub,
        hub.bus,
        spool=tmp_path / "spool",
        writer=writer,  # type: ignore[arg-type]
        unit_state=unit_state,
        version="test",
        runtime=ops.runtime,
    )
    public = build_public_app(
        store=store,
        manager=SessionManager(store, None, None),  # type: ignore[arg-type]
        hub=hub,
        rooms=None,  # type: ignore[arg-type]
        unit_state=unit_state,
        maintenance=lambda: False,
        version="test",
        commands=commands,
        diagnostics=diagnostics,
    )
    runner, port = await _start(public)
    internal_runner, internal_port = await _start(
        build_internal_app(hub=hub, token="t", store=store)
    )
    _, token = pair(store, master=True)
    _, desktop_token = pair(store, "desktop")
    spotify = SlowSpotify()
    client = DesktopClient(
        f"http://127.0.0.1:{port}",
        desktop_token,
        {
            "spotify.control": spotify.control,
            "spotify.now_playing": spotify.status,
            "diagnostics.snapshot": spotify.snapshot,
        },
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(client.run_forever(stop))
    await _wait(lambda: bool(hub.status()["capabilities"]))
    async with aiohttp.ClientSession() as http:
        yield {
            "http": http,
            "url": f"http://127.0.0.1:{port}",
            "internal": f"http://127.0.0.1:{internal_port}",
            "headers": {"Authorization": f"Bearer {token}"},
            "store": store,
            "ops": ops,
            "spotify": spotify,
            "writer": writer,
            "diagnostics": diagnostics,
            "stop": stop,
            "hub": hub,
        }
    stop.set()
    spotify.release.set()
    await asyncio.wait_for(task, 5)
    await internal_runner.cleanup()
    await runner.cleanup()


async def test_status_answers_while_spotify_hangs_and_stop_really_stops(
    chain, monkeypatch
) -> None:
    from src.jarvis_control import operations

    monkeypatch.setattr(operations, "UNCONFIRMED_AFTER", 3)
    http, url, headers = chain["http"], chain["url"], chain["headers"]
    command = asyncio.ensure_future(
        http.post(
            f"{url}/v1/commands",
            json={
                "id": "op-resume-1",
                "kind": "music.control",
                "params": {"action": "resume"},
            },
            headers=headers,
        )
    )
    await asyncio.wait_for(chain["spotify"].started.wait(), 5)

    # Status is answered at once, from the operation record.
    runtime = await asyncio.wait_for(
        (await http.get(f"{url}/v1/runtime", headers=headers)).json(), 2
    )
    [active] = runtime["operations"]["active"]
    assert active["id"] == "op-resume-1"
    assert active["stage"] == "waiting for Spotify on the computer"
    # A read of the music state does not queue behind the stuck command either.
    status = await asyncio.wait_for(
        http.post(
            f"{url}/v1/commands",
            json={"id": "s1", "kind": "music.status"},
            headers=headers,
        ),
        3,
    )
    assert status.status == 200

    cancel = await (
        await http.post(f"{url}/v1/operations/op-resume-1/cancel", headers=headers)
    ).json()
    assert cancel["state"] == "cancelling"
    response = await asyncio.wait_for(command, 5)
    assert response.status == 409  # stopping was requested, not yet confirmed
    await _wait(lambda: chain["spotify"].cancelled)  # the PC really stopped the action
    await _wait(lambda: chain["ops"].get("op-resume-1")["state"] == "cancelled")
    assert chain["store"].operations(since=0)[0]["state"] == "cancelled"


async def test_cancel_while_queued_and_cancel_all(chain) -> None:
    http, url, headers = chain["http"], chain["url"], chain["headers"]
    first = asyncio.ensure_future(
        http.post(
            f"{url}/v1/commands",
            json={"id": "a", "kind": "music.control", "params": {"action": "next"}},
            headers=headers,
        )
    )
    await asyncio.wait_for(chain["spotify"].started.wait(), 5)
    second = asyncio.ensure_future(
        http.post(
            f"{url}/v1/commands",
            json={"id": "b", "kind": "music.control", "params": {"action": "previous"}},
            headers=headers,
        )
    )
    await _wait(lambda: (chain["ops"].get("b") or {}).get("state") == "queued")
    stopped = await (
        await http.post(f"{url}/v1/operations/cancel-all", headers=headers)
    ).json()
    assert {o["id"] for o in stopped["operations"]} == {"a", "b"}
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    # "b" never reached the PC: cancelled at once, and "previous" was never sent.
    assert chain["ops"].get("b")["state"] == "cancelled"
    assert chain["spotify"].calls == ["next"]


async def test_late_success_is_reported_honestly(chain) -> None:
    http, url, headers = chain["http"], chain["url"], chain["headers"]
    command = asyncio.ensure_future(
        http.post(
            f"{url}/v1/commands",
            json={"id": "c", "kind": "music.control", "params": {"action": "resume"}},
            headers=headers,
        )
    )
    await asyncio.wait_for(chain["spotify"].started.wait(), 5)
    op = chain["ops"].get("c")
    # The PC finishes before it hears about the cancel: the effect happened.
    chain["spotify"].release.set()
    await asyncio.wait_for(command, 5)
    assert chain["ops"].get("c")["state"] == "completed"
    again = await (
        await http.post(f"{url}/v1/operations/c/cancel", headers=headers)
    ).json()
    assert again["state"] == "completed"  # nothing to stop any more
    assert op["cancel_requested_at"] is None


def test_report_names_are_only_the_timestamp() -> None:
    name = report_name(2_000_000_000.123456)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d\.\d{6}-0300", name), name
    assert name.startswith("2033-05-18T00-33-20.12345")


def test_secrets_are_removed() -> None:
    text, removed = redact(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdefgh token=jd1.ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 ok"
    )
    assert "eyJ" not in text and "jd1." not in text and removed >= 2
    assert text.endswith("ok")


async def test_report_is_collected_saved_and_announced(chain) -> None:
    http, url, headers, store = (
        chain["http"],
        chain["url"],
        chain["headers"],
        chain["store"],
    )
    # The agent recorded part of a call.
    await http.post(
        f"{chain['internal']}/internal/v1/conversation",
        json={
            "items": [
                {
                    "room": "jarvis-x",
                    "kind": "message",
                    "role": "user",
                    "text": "continue the music",
                    "at": __import__("time").time(),
                },
                {
                    "room": "jarvis-x",
                    "kind": "tool",
                    "role": "tool",
                    "text": "control_spotify",
                    "data": {"output": "timeout"},
                    "at": __import__("time").time(),
                },
            ]
        },
        headers={"Authorization": "Bearer t"},
    )
    phone = {
        "voice": "processing",
        "log": ["11:00:01: Starting session connection…", "password: hunter22"],
    }
    created = await http.post(
        f"{url}/v1/diagnostic-reports",
        json={"note": "Spotify froze", "phone": phone, "request_id": "r1"},
        headers=headers,
    )
    assert created.status == 202
    report = await created.json()
    again = await (
        await http.post(
            f"{url}/v1/diagnostic-reports", json={"request_id": "r1"}, headers=headers
        )
    ).json()
    assert again["id"] == report["id"]  # the same request twice: one report

    await _wait(
        lambda: (store.report(report["id"]) or {}).get("published") == 1, timeout=30
    )
    saved = store.report(report["id"])
    name = saved["name"]
    files = chain["writer"].files
    assert set(files) == {f"Relatorios/{name}.md", f"Relatorios/{name}.zip"}
    markdown = files[f"Relatorios/{name}.md"].decode()
    assert "Spotify froze" in markdown and "BATCOMPUTADOR | collected" in markdown
    # On Windows there is no systemd: those sources are listed as gaps.
    assert saved["state"] in {"partial", "ready"}
    with zipfile.ZipFile(io.BytesIO(files[f"Relatorios/{name}.zip"])) as archive:
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "phone/app.log",
            "desktop/desktop.log",
            "conversations/conversation.md",
        } <= names
        everything = b"".join(archive.read(n) for n in names)
    assert b"hunter22" not in everything and b"abcdef123456" not in everything
    assert b"continue the music" in everything
    manifest = json.loads(
        zipfile.ZipFile(io.BytesIO(files[f"Relatorios/{name}.zip"])).read(
            "manifest.json"
        )
    )
    assert {s["source"] for s in manifest["sources"]} >= {
        "phone",
        "BATCOMPUTADOR",
        "conversation",
    }
    events = store.events_after(0)
    assert events[-1]["kind"] == "diagnostic_ready"
    md = await http.get(
        f"{url}/v1/diagnostic-reports/{report['id']}/md", headers=headers
    )
    assert md.status == 200


async def test_report_waits_in_the_spool_when_nextcloud_is_down(chain) -> None:
    http, url, headers, store = (
        chain["http"],
        chain["url"],
        chain["headers"],
        chain["store"],
    )
    chain["writer"].fail = True
    chain["stop"].set()  # and the PC is offline
    await _wait(lambda: not chain["hub"].status()["online"])
    report = await (
        await http.post(
            f"{url}/v1/diagnostic-reports", json={"phone": {"log": []}}, headers=headers
        )
    ).json()
    await _wait(
        lambda: store.report(report["id"])["state"] in {"partial", "ready"}, timeout=40
    )
    saved = store.report(report["id"])
    assert saved["state"] == "partial" and saved["published"] == 0
    assert "not published yet" in saved["error"]
    desktop = next(
        s for s in saved["summary"]["sources"] if s["source"] == "BATCOMPUTADOR"
    )
    assert desktop["status"] != "collected"
    chain["writer"].fail = False
    await chain["diagnostics"].tick()
    assert store.report(report["id"])["published"] == 1


def test_notes_writer_stays_in_its_folders() -> None:
    writer = NotesWriter("https://cloud.example", "jarvis-reader", "pw")
    assert writer._url("Relatorios/x.md").endswith(
        "/files/jarvis-reader/JARVIS-Notas/Relatorios/x.md"
    )
    for bad in ("../x.md", "Relatorios/../../etc", "Outros/x.md", "/Relatorios/../x"):
        with pytest.raises(NotesError):
            writer._url(bad)


async def test_spotify_cli_reaps_a_cancelled_process(monkeypatch) -> None:
    killed = []

    class Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(60)

        def kill(self):
            killed.append(True)
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cli = spotify_cli.SpotifyCli()
    cli._cli = "spotify_cli"
    task = asyncio.ensure_future(cli._run("resume"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True]


async def test_restart_closes_operations_left_running(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    hub = DesktopHub(store)
    ops = Operations(store, hub.bus)
    ops.start("phone", "music.control", "Music: resume")
    store.close()
    store = Store(tmp_path / "db.sqlite")
    Operations(store, DesktopHub(store).bus)
    [op] = store.operations(since=0)
    assert op["state"] == "stalled" and "restarted" in op["stage"]
    _ = web  # keep the import for fixtures


async def test_agent_conversation_reaches_the_server(chain, monkeypatch) -> None:
    import time as _time
    from types import SimpleNamespace

    from src import conversation_log

    monkeypatch.setattr(conversation_log, "FLUSH_SECONDS", 0.05)

    class Session:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def on(self, name: str):
            def register(fn):
                self.handlers[name] = fn
                return fn

            return register

    session = Session()
    log = conversation_log.ConversationLog(chain["internal"], "t", "jarvis-room")
    log.attach(session)
    session.handlers["conversation_item_added"](
        SimpleNamespace(
            item=SimpleNamespace(
                role="user", text_content="continue a música", interrupted=False
            )
        )
    )
    session.handlers["function_tools_executed"](
        SimpleNamespace(
            function_calls=[
                SimpleNamespace(name="control_spotify", arguments='{"action":"resume"}')
            ],
            function_call_outputs=[
                SimpleNamespace(output="Spotify: resume done.", is_error=False)
            ],
        )
    )
    session.handlers["agent_state_changed"](
        SimpleNamespace(old_state="thinking", new_state="speaking")
    )
    await log.aclose()
    items = chain["store"].conversation_items(0, _time.time() + 1)
    assert [i["kind"] for i in items] == ["message", "tool", "state"]
    assert items[1]["data"]["output"] == "Spotify: resume done."
