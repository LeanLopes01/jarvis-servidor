"""jarvis-control: session lifecycle with fake systemd/LiveKit, and the HTTP API."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.sessions import BusyError, NotFoundError, SessionManager, Timing
from src.jarvis_control.store import Store
from src.jarvis_control.web import RateLimiter, build_public_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeUnit:
    def __init__(self) -> None:
        self.unit_state = "inactive"
        self.ready = False
        self.starts = 0
        self.stops = 0

    async def state(self) -> str:
        return self.unit_state

    async def start(self) -> None:
        self.starts += 1
        self.unit_state = "active"

    async def stop(self) -> None:
        self.stops += 1
        self.unit_state = "inactive"
        self.ready = False

    async def is_ready(self) -> bool:
        return self.unit_state == "active" and self.ready


class FakeRooms:
    url = "wss://example.livekit.cloud"

    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.deleted: list[str] = []
        self.users_by_room: dict[str, list[str] | None] = {}
        self.fail = False

    async def prepare(self, room: str, metadata: str | None = None) -> None:
        self.prepared.append(room)
        self.metadata = metadata
        self.users_by_room.setdefault(room, [])

    async def users(self, room: str) -> list[str] | None:
        if self.fail:
            raise RuntimeError("LiveKit unreachable")
        return self.users_by_room.get(room)

    async def delete(self, room: str) -> None:
        self.deleted.append(room)
        self.users_by_room[room] = None

    def token(self, room: str, identity: str, name: str) -> str:
        return f"token-for-{room}-{identity}"


@pytest.fixture
def parts():
    clock = Clock()
    store = Store(":memory:")
    unit = FakeUnit()
    rooms = FakeRooms()
    manager = SessionManager(store, unit, rooms, timing=Timing(), clock=clock)
    return clock, store, unit, rooms, manager


async def _ready_session(parts, device="phone"):
    clock, _, unit, _, manager = parts
    session = await manager.create(device, None)
    unit.ready = True
    clock.advance(4)
    await manager.tick()
    return manager.get(device, session.id)


async def test_start_dispatch_and_stop_after_client_leaves(parts) -> None:
    clock, _, unit, rooms, manager = parts
    session = await manager.create("phone", "key-1")
    assert session.state == "starting"
    assert unit.starts == 1

    await manager.tick()  # registered? not yet
    assert manager.get("phone", session.id).state == "starting"

    unit.ready = True
    clock.advance(4)
    await manager.tick()
    assert manager.get("phone", session.id).state == "ready"
    assert rooms.prepared == [session.room]

    rooms.users_by_room[session.room] = ["app-phone"]
    clock.advance(2)
    await manager.tick()
    assert manager.get("phone", session.id).state == "active"

    # Short network loss: within the reconnect grace nothing ends.
    rooms.users_by_room[session.room] = []
    clock.advance(60)
    await manager.tick()
    assert manager.get("phone", session.id).state == "active"

    clock.advance(31)
    await manager.tick()
    ended = manager.get("phone", session.id)
    assert (ended.state, ended.end_reason) == ("ended", "client_left")
    assert session.room in rooms.deleted

    # The agent stops only after the idle grace with no live sessions.
    clock.advance(10)
    await manager.tick()
    assert unit.stops == 0
    clock.advance(25)
    await manager.tick()
    assert unit.stops == 1


async def test_explicit_end_and_idempotent_create(parts) -> None:
    _, _, unit, rooms, manager = parts
    first = await manager.create("phone", "same-key")
    again = await manager.create("phone", "same-key")
    assert again.id == first.id
    assert unit.starts == 1

    await manager.end("phone", first.id, "client_ended")
    await manager.end("phone", first.id, "client_ended")  # idempotent
    assert manager.get("phone", first.id).end_reason == "client_ended"
    assert rooms.deleted == [first.room]


async def test_one_session_at_a_time(parts) -> None:
    _, _, _, _, manager = parts
    first = await manager.create("phone", None)
    with pytest.raises(BusyError):
        await manager.create("tablet", None)
    # The same device starting again replaces its old session.
    second = await manager.create("phone", None)
    assert manager.get("phone", first.id).end_reason == "replaced"
    assert manager.get("phone", second.id).state == "starting"
    with pytest.raises(NotFoundError):
        manager.get("tablet", second.id)


async def test_start_timeout_and_client_never_joining(parts) -> None:
    clock, _, unit, _, manager = parts
    stuck = await manager.create("phone", None)
    unit.unit_state = "activating"
    clock.advance(61)
    await manager.tick()
    failed = manager.get("phone", stuck.id)
    assert (failed.state, failed.end_reason) == ("failed", "agent_start_timeout")

    session = await _ready_session(parts)
    clock.advance(91)
    await manager.tick()
    assert manager.get("phone", session.id).end_reason == "client_never_joined"


async def test_heartbeat_alone_does_not_keep_an_empty_room(parts) -> None:
    clock, _, _, rooms, manager = parts
    session = await _ready_session(parts)
    rooms.users_by_room[session.room] = ["app-phone"]
    await manager.tick()
    rooms.users_by_room[session.room] = []
    for _ in range(10):
        clock.advance(10)
        manager.heartbeat("phone", session.id)
        await manager.tick()
    assert manager.get("phone", session.id).end_reason == "client_left"


async def test_livekit_outage_keeps_session_while_heartbeats_arrive(parts) -> None:
    clock, _, _, rooms, manager = parts
    session = await _ready_session(parts)
    rooms.users_by_room[session.room] = ["app-phone"]
    await manager.tick()

    rooms.fail = True
    for _ in range(70):
        clock.advance(10)
        manager.heartbeat("phone", session.id)
        await manager.tick()
    assert manager.get("phone", session.id).state == "active"

    clock.advance(700)  # heartbeats stopped too
    await manager.tick()
    assert manager.get("phone", session.id).end_reason == "livekit_unreachable"


async def test_room_closed_by_agent_and_agent_crash(parts) -> None:
    clock, _, unit, rooms, manager = parts
    session = await _ready_session(parts)
    rooms.users_by_room[session.room] = None  # EndCallTool deleted the room
    clock.advance(2)
    await manager.tick()
    assert manager.get("phone", session.id).end_reason == "room_closed"

    crashed = await _ready_session(parts)
    unit.unit_state = "failed"
    clock.advance(2)
    await manager.tick()
    assert manager.get("phone", crashed.id).end_reason == "agent_stopped"


async def test_agent_left_running_after_restart_is_stopped(parts) -> None:
    clock, _, unit, _, manager = parts
    unit.unit_state = "active"  # e.g. control restarted with no sessions
    await manager.tick()
    assert unit.stops == 0
    clock.advance(31)
    await manager.tick()
    assert unit.stops == 1


@pytest.fixture
async def api(parts):
    _, store, unit, rooms, manager = parts

    async def unit_state() -> str:
        return await unit.state()

    app = build_public_app(
        store=store,
        manager=manager,
        hub=DesktopHub(store),
        rooms=rooms,  # type: ignore[arg-type]
        unit_state=unit_state,
        maintenance=lambda: False,
        version="test",
        limiter=RateLimiter(),
    )
    async with TestClient(TestServer(app)) as client:
        yield client, store, unit


async def test_http_pairing_and_session_flow(api) -> None:
    client, store, unit = api
    assert (await client.get("/v1/health")).status == 200
    assert (await client.post("/v1/sessions")).status == 401

    bad = await client.post("/v1/pair", json={"code": "AAAA-BBBB", "name": "Pixel"})
    assert bad.status == 400

    code = store.create_pairing_code("app", 600, 2_000_000_000)
    paired = await client.post("/v1/pair", json={"code": code.lower(), "name": "Pixel"})
    assert paired.status == 201
    token = (await paired.json())["device_token"]
    reused = await client.post("/v1/pair", json={"code": code, "name": "Again"})
    assert reused.status == 400  # single use

    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "tap-1"}
    created = await client.post("/v1/sessions", headers=headers)
    assert created.status == 202
    session = await created.json()
    assert session["state"] == "starting"
    assert "connection" not in session

    unit.ready = True
    response = await client.get(f"/v1/sessions/{session['id']}", headers=headers)
    assert (await response.json())["state"] == "starting"

    # A desktop credential cannot use the phone endpoints.
    desktop_code = store.create_pairing_code("desktop", 600, 2_000_000_000)
    desktop = await (
        await client.post("/v1/pair", json={"code": desktop_code, "name": "PC"})
    ).json()
    denied = await client.post(
        "/v1/sessions", headers={"Authorization": f"Bearer {desktop['device_token']}"}
    )
    assert denied.status == 401

    ended = await client.delete(f"/v1/sessions/{session['id']}", headers=headers)
    assert ended.status == 204
    assert (
        await client.delete(f"/v1/sessions/{session['id']}", headers=headers)
    ).status == 204


async def test_http_session_ready_includes_connection(api, parts) -> None:
    client, store, unit = api
    clock, *_, manager = parts
    code = store.create_pairing_code("app", 600, 2_000_000_000)
    token = (await (await client.post("/v1/pair", json={"code": code})).json())[
        "device_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    session = await (await client.post("/v1/sessions", headers=headers)).json()

    unit.ready = True
    clock.advance(4)
    await manager.tick()
    ready = await (
        await client.get(f"/v1/sessions/{session['id']}", headers=headers)
    ).json()
    assert ready["state"] == "ready"
    assert ready["connection"]["server_url"] == FakeRooms.url
    assert ready["connection"]["participant_token"].startswith("token-for-jarvis-")


async def test_http_rate_limits_failed_auth(api) -> None:
    client, _, _ = api
    for _ in range(20):
        await client.get("/v1/status", headers={"Authorization": "Bearer wrong"})
    limited = await client.get("/v1/status", headers={"Authorization": "Bearer wrong"})
    assert limited.status == 429
