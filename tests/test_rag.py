"""DiceVTT RAG through JARVIS: phone → server → helper → local RAG service, the helper's
supervision of the RAG's life cycle, and the voice tool's condensed result."""

import asyncio
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.sessions import SessionManager
from src.jarvis_control.store import Store
from src.jarvis_control.web import build_public_app
from src.jarvis_desktop.actions import rag_actions
from src.jarvis_desktop.client import DesktopClient
from src.jarvis_desktop.rag_service import RagStack
from src.project_knowledge import SOURCE_TEXT_LIMIT, TOTAL_TEXT_LIMIT, condense
from tests.test_desktop import _start
from tests.test_master_phone import pair

TOKEN = "t" * 40


def fake_rag_service(seen: list[dict[str, Any]]) -> web.Application:
    """Stands in for unity_rag's HTTP service (127.0.0.1, X-Local-Token)."""

    @web.middleware
    async def token(request: web.Request, handler):
        if request.headers.get("X-Local-Token") != TOKEN:
            return web.json_response(
                {"message": "Token local ausente ou inválido."}, status=401
            )
        return await handler(request)

    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "service": "unity-rag",
                "online": True,
                "starting": False,
                "index": {"state": "ready", "code_pointers": 3},
                "model": {"state": "unloaded"},
            }
        )

    async def retrieve(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append(body)
        return web.json_response(
            {
                "query": body["query"],
                "warnings": [],
                "sources": [
                    {
                        "id": "S1",
                        "kind": "code_pointer",
                        "relpath": "Assets/Scripts/Salas/SteamLobbyManager.cs",
                        "text": "public class SteamLobbyManager {}",
                        "provenance": {"main_class": "SteamLobbyManager"},
                        "read_at": "2026-10-21T20:00:00-03:00",
                    },
                ],
            }
        )

    async def asks(request: web.Request) -> web.Response:
        seen.append(await request.json())
        return web.json_response({"ask_id": "abc123", "state": "queued"}, status=202)

    async def ask(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": request.match_info["ask_id"],
                "state": "running",
                "progress": {
                    "stage": "generating",
                    "message": "Gerando resposta...",
                    "elapsed_ms": 4000,
                },
            }
        )

    async def bad_query(request: web.Request) -> web.Response:
        return web.json_response(
            {"message": "Job desconhecido ou expirado."}, status=404
        )

    app = web.Application(middlewares=[token])
    app.add_routes(
        [
            web.get("/v1/rag/health", health),
            web.post("/v1/rag/retrieve", retrieve),
            web.post("/v1/rag/asks", asks),
            web.get("/v1/rag/asks/{ask_id}", ask),
            web.get("/v1/rag/index-jobs/{job_id}", bad_query),
        ]
    )
    return app


def make_stack(tmp_path: Path, rag_port: int) -> RagStack:
    root = tmp_path / "Orquestrador"
    (root / ".venv-rag" / "Scripts").mkdir(parents=True)
    (root / ".venv-rag" / "Scripts" / "python.exe").write_text("")
    (root / ".local" / "rag").mkdir(parents=True)
    (root / ".local" / "ingestion").mkdir(parents=True)
    (root / ".local" / "rag.toml").write_text("")
    (root / ".local" / "ingestion.toml").write_text("")
    (root / ".local" / "rag" / "api-token").write_text(TOKEN)
    stack = RagStack(root)
    stack.services["rag"].port = rag_port
    stack.services["ingestion"].port = 9  # nothing listens: reported offline
    return stack


@pytest.fixture
async def chain(tmp_path: Path):
    seen: list[dict[str, Any]] = []
    rag_runner, rag_port = await _start(fake_rag_service(seen))
    store = Store(":memory:")
    hub = DesktopHub(store)

    async def unit_state() -> str:
        return "inactive"

    app = build_public_app(
        store=store,
        manager=SessionManager(store, None, None),
        hub=hub,
        rooms=None,
        unit_state=unit_state,
        maintenance=lambda: False,  # type: ignore[arg-type]
        version="test",
    )
    runner, port = await _start(app)
    phone, phone_token = pair(store)
    _, desktop_token = pair(store, "desktop")
    stack = make_stack(tmp_path, rag_port)
    client = DesktopClient(
        f"http://127.0.0.1:{port}", desktop_token, rag_actions(stack)
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(client.run_forever(stop))
    for _ in range(100):
        if hub.status()["capabilities"]:
            break
        await asyncio.sleep(0.02)
    async with aiohttp.ClientSession() as http:
        yield {
            "http": http,
            "url": f"http://127.0.0.1:{port}",
            "store": store,
            "phone": phone,
            "headers": {"Authorization": f"Bearer {phone_token}"},
            "seen": seen,
            "stop": stop,
        }
    stop.set()
    await task
    await runner.cleanup()
    await rag_runner.cleanup()


async def test_phone_needs_work_access_for_the_rag(chain) -> None:
    response = await chain["http"].get(
        chain["url"] + "/v1/rag/health", headers=chain["headers"]
    )
    assert response.status == 403
    chain["store"].set_work_access(chain["phone"], True)
    response = await chain["http"].get(
        chain["url"] + "/v1/rag/health", headers=chain["headers"]
    )
    body = await response.json()
    assert response.status == 200
    assert body["online"] is True
    assert body["rag"]["index"]["code_pointers"] == 3
    assert body["ingestion"] == {"online": False}


async def test_retrieve_and_ask_reach_the_local_rag(chain) -> None:
    chain["store"].set_work_access(chain["phone"], True)
    http, url, headers = chain["http"], chain["url"], chain["headers"]
    response = await http.post(
        url + "/v1/rag/retrieve",
        headers=headers,
        json={"query": "Como o lobby é criado?"},
    )
    body = await response.json()
    assert response.status == 200
    assert body["sources"][0]["relpath"].endswith("SteamLobbyManager.cs")
    assert chain["seen"][0] == {
        "project_id": "dicevtt",
        "query": "Como o lobby é criado?",
        "text_limit": 6000,
    }

    response = await http.post(
        url + "/v1/rag/asks",
        headers=headers,
        json={"query": "Explique", "request_id": "r-1"},
    )
    assert response.status == 202
    assert (await response.json())["ask_id"] == "abc123"
    assert chain["seen"][1]["request_id"] == "r-1"
    response = await http.get(url + "/v1/rag/asks/abc123", headers=headers)
    assert (await response.json())["progress"]["stage"] == "generating"


async def test_rag_errors_are_reported_not_hidden(chain) -> None:
    chain["store"].set_work_access(chain["phone"], True)
    http, url, headers = chain["http"], chain["url"], chain["headers"]
    response = await http.post(
        url + "/v1/rag/retrieve", headers=headers, json={"query": " "}
    )
    assert response.status == 400
    response = await http.post(
        url + "/v1/rag/retrieve",
        headers=headers,
        json={"query": "x", "project_id": "other"},
    )
    assert response.status == 400
    response = await http.get(url + "/v1/rag/index-jobs/zzz", headers=headers)
    assert response.status == 502
    assert "desconhecido" in (await response.json())["message"]


async def test_pc_offline_is_a_clear_503(chain) -> None:
    chain["store"].set_work_access(chain["phone"], True)
    chain["stop"].set()
    await asyncio.sleep(0.2)
    response = await chain["http"].post(
        chain["url"] + "/v1/rag/retrieve",
        headers=chain["headers"],
        json={"query": "grid"},
    )
    assert response.status == 503
    assert (await response.json())["error"] == "pc_offline"


class FakeProcess:
    def __init__(self, code: int | None) -> None:
        self.code = code
        self.pid = 4242

    def poll(self) -> int | None:
        return self.code


async def test_rag_turned_off_on_purpose_stops_the_helper(tmp_path: Path) -> None:
    stack = make_stack(tmp_path, 9)
    service = stack.services["rag"]
    service.process = FakeProcess(0)  # type: ignore[assignment]
    stop = asyncio.Event()
    await stack._check(service, stop)
    assert stop.is_set() and stack.stopped_on_purpose
    assert service.state == "stopped"


async def test_rag_crash_is_restarted_a_few_times_then_reported(
    tmp_path: Path, monkeypatch
) -> None:
    stack = make_stack(tmp_path, 9)
    service = stack.services["rag"]
    started: list[str] = []

    async def fake_start(svc) -> None:
        started.append(svc.name)
        svc.process = FakeProcess(1)

    monkeypatch.setattr(stack, "_start_one", fake_start)
    service.process = FakeProcess(1)  # type: ignore[assignment]
    stop = asyncio.Event()
    for _ in range(5):
        await stack._check(service, stop)
    assert not stop.is_set()
    assert started == ["rag", "rag", "rag"]
    assert service.state == "error" and "restart limit" in (service.error or "")


async def test_ingestion_exit_does_not_stop_the_helper(tmp_path: Path) -> None:
    stack = make_stack(tmp_path, 9)
    service = stack.services["ingestion"]
    service.process = FakeProcess(0)  # type: ignore[assignment]
    stop = asyncio.Event()
    await stack._check(service, stop)
    assert not stop.is_set()


def test_stack_without_install_offers_no_rag(tmp_path: Path) -> None:
    from src.jarvis_desktop.actions import build_actions

    stack = RagStack(tmp_path / "missing")
    assert not stack.available
    assert not any(name.startswith("rag.") for name in build_actions(rag=stack))
    assert "rag.retrieve" in build_actions(rag=make_stack(tmp_path, 9))


def test_voice_result_is_condensed() -> None:
    long = "x" * 10_000
    result = condense(
        {
            "warnings": ["a"] * 10,
            "sources": [
                {
                    "id": f"S{i}",
                    "kind": "documentation",
                    "relpath": f"d{i}.md",
                    "text": long,
                    "provenance": {"source_url": "https://docs.unity3d.com/x"},
                }
                for i in range(6)
            ],
        }
    )
    excerpts = [s["excerpt"] for s in result["sources"]]
    assert all(len(e) <= SOURCE_TEXT_LIMIT for e in excerpts)
    assert sum(len(e) for e in excerpts) <= TOTAL_TEXT_LIMIT
    assert result["sources"][0]["excerpt_truncated"] is True
    assert result["sources"][0]["origin"] == "https://docs.unity3d.com/x"
    assert len(result["warnings"]) == 6
