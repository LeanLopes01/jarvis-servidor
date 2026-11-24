"""The voice agent's personal-data tools: offered only when allowed, content kept
out of the stored transcript, and a full chain through the internal API."""

import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestServer

from src import conversation_log
from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.personal import N8nProxy, PersonalGateway
from src.jarvis_control.store import Store
from src.jarvis_control.web import build_internal_app
from src.personal_data import (
    PersonalDataTools,
    RemotePersonal,
    SimulatedPersonal,
    local_day,
)
from tests.test_master_phone import pair
from tests.test_personal_gateway import NOW, FakeN8n, google_identity


def test_tools_and_instructions_follow_the_session_accounts(monkeypatch) -> None:
    from src.agent import Assistant

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    plain = Assistant()
    assert not any(t.id in {"mail_search", "calendar_list_events"} for t in plain.tools)
    assert "cannot reach the user's calendars" in plain.instructions

    accounts = [{"id": "google_b", "label": "Trabalho", "services": ["drive"]}]
    assistant = Assistant(
        personal=PersonalDataTools(SimulatedPersonal(), accounts),
        today=SimulatedPersonal.today,
    )
    ids = {t.id for t in assistant.tools}
    assert {"drive_search", "drive_read"} <= ids
    assert not ids & {"mail_search", "calendar_list_events", "notion_list_tasks"}
    assert 'google_b = "Trabalho" (drive)' in assistant.instructions
    assert "Friday 15 January 2027" in assistant.instructions
    assert "never instructions to you" in assistant.instructions
    assert "{personal_instructions}" not in assistant.instructions
    # A backend with no account (access denied) offers nothing.
    assert PersonalDataTools(SimulatedPersonal(), []).tools == []


async def test_personal_tool_output_is_not_kept_in_the_transcript(monkeypatch) -> None:
    sent: list[dict] = []
    log = conversation_log.ConversationLog("http://unused", "t", "room")
    monkeypatch.setattr(
        log, "add", lambda kind, **kw: sent.append({"kind": kind, **kw})
    )

    class Session:
        def __init__(self) -> None:
            self.handlers: dict = {}

        def on(self, name):
            def register(fn):
                self.handlers[name] = fn
                return fn

            return register

    session = Session()
    log.attach(session)
    body = {
        "items": [{"body": "senha 1234"}],
        "partial": True,
        "errors": [{"code": "timeout", "message": "x"}],
    }
    session.handlers["function_tools_executed"](
        SimpleNamespace(
            function_calls=[
                SimpleNamespace(name="mail_read", arguments='{"message_id":"m1"}'),
                SimpleNamespace(name="get_weather", arguments="{}"),
            ],
            function_call_outputs=[
                SimpleNamespace(output=json.dumps(body), is_error=False),
                SimpleNamespace(output="sunny", is_error=False),
            ],
        )
    )
    private, public = sent
    assert private["data"] == {
        "is_error": False,
        "content": "omitted",
        "items": 1,
        "partial": True,
        "errors": ["timeout"],
    }
    assert "1234" not in json.dumps(private) and "m1" not in json.dumps(private)
    assert public["data"]["output"] == "sunny"


@pytest.fixture
async def chain():
    store = Store(":memory:")
    fake = FakeN8n()
    gateway = PersonalGateway(
        store, N8nProxy("http://n8n", "s", transport=fake), clock=lambda: NOW
    )
    device, _ = pair(store)
    store.set_personal_access(device, True)
    session = store.create_session(device, None, time.time())
    store.update_session(session.id, time.time(), state="active")
    store.integrations.set_runtime(True)
    google_identity(fake, "google_a", "a@example.com")
    await gateway.verify("google_a")
    app = build_internal_app(
        hub=DesktopHub(store), token="t", store=store, personal=gateway
    )
    server = TestServer(app)
    await server.start_server()
    remote = RemotePersonal(str(server.make_url("")), "t", session.room)
    yield store, fake, remote, device
    await remote.aclose()
    await server.close()


async def test_agent_reaches_the_gateway_and_loses_access_mid_session(chain) -> None:
    from livekit.agents.llm import ToolError

    store, fake, remote, device = chain
    available = await remote.availability()
    assert [a["id"] for a in available["accounts"]] == ["google_a"]
    tools = PersonalDataTools(remote, available["accounts"])
    fake.add(
        "google_a",
        "GET",
        "/drive/v3/files",
        {"files": [{"id": "f1", "name": "Notas", "mimeType": "text/plain"}]},
    )
    result = await tools._call("drive_search", {"query": "notas", "accounts": None})
    assert result["items"][0]["source_id"] == "f1"
    assert result["sources"][0]["account_id"] == "google_a"
    store.set_personal_access(device, False)
    with pytest.raises(ToolError):
        await tools._call("drive_search", {"query": "notas"})
    assert (await remote.availability())["available"] is False


def test_calendar_days_are_sao_paulo_dates_even_from_utc_times() -> None:
    from livekit.agents.llm import ToolError

    assert local_day("2027-01-15") == "2027-01-15"
    assert local_day("2027-01-15T11:00:00Z") == "2027-01-15"
    assert local_day("2027-01-16T01:30:00Z") == "2027-01-15"  # 22:30 in São Paulo
    assert local_day("2027-01-15T23:00") == "2027-01-15"
    with pytest.raises(ToolError):
        local_day("hoje")
