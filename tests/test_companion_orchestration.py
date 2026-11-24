import asyncio
import json
import sys

import pytest

from src.coding_agents import RemoteCoding
from src.desktop_link import DesktopLinkError
from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.orchestrator import Orchestrator, OrchestratorError
from src.jarvis_control.store import Store
from src.jarvis_desktop.agent_jobs import JobManager
from tests.test_master_phone import pair
from tests.test_orchestration import orch  # noqa: F401


def test_work_role_does_not_grant_master_or_change_motorola():
    store = Store(":memory:")
    motorola, _ = pair(store, master=True)
    samsung, _ = pair(store)
    desktop, _ = pair(store, "desktop")
    assert not store.can_work(samsung)
    assert store.set_work_access(samsung, True)
    assert store.set_work_access(desktop, True)
    assert store.can_work(samsung) and store.can_work(desktop)
    assert not store.profile(samsung).master
    assert store.profile(motorola).master
    assert store.profile(samsung).view()["work_access"]
    store.set_work_access(samsung, False)
    assert not store.can_work(samsung)


async def test_offline_request_is_not_queued_or_replayed():
    store = Store(":memory:")
    hub = DesktopHub(store)
    orchestrator = Orchestrator(
        ":memory:", store, hub, hub.bus, require_local_runtime=True
    )
    with pytest.raises(OrchestratorError, match="Abra o Orquestrador") as exc:
        await orchestrator.post_message(
            "phone", "dicevtt", "Change the grid", request_id="r1"
        )
    assert exc.value.status == 503
    assert orchestrator.conversations() == []
    assert orchestrator._rows("SELECT * FROM jobs") == []


async def test_companion_has_projects_and_reports_but_not_social_ingestion(orch):  # noqa: F811
    device, token = pair(orch["store"])
    headers = {"Authorization": f"Bearer {token}"}
    http, url = orch["http"], orch["url"]
    response = await http.get(url + "/v1/conversations", headers=headers)
    assert response.status == 403
    orch["store"].set_work_access(device, True)
    for path in ("/v1/conversations", "/v1/events"):
        response = await http.get(url + path, headers=headers)
        assert response.status == 200
    response = await http.post(
        url + "/v1/mobile-notifications", headers=headers, json={}
    )
    assert response.status == 403


async def test_intermediate_progress_is_saved_without_finishing_job(orch):  # noqa: F811
    o = orch["orchestrator"]
    o._db.execute(
        "INSERT INTO conversations (id,project,device_id,title,state,created_at,updated_at) VALUES ('c','dicevtt','d','Grid','running',1,1)"
    )
    o._db.execute(
        "INSERT INTO jobs (id,purpose,conversation_id,state,params,created_at) VALUES ('j','task','c','running',?,1)",
        (json.dumps({"agent": "codex"}),),
    )
    o.record_job_update(
        {
            "id": "j",
            "state": "running",
            "progress": {"stdout_tail": "Checking grid", "unexpected": "ignored"},
        }
    )
    result = o.progress()["requests"][0]
    assert result["jobs"][0]["progress"] == {"stdout_tail": "Checking grid"}
    assert result["jobs"][0]["agent"] == "codex"
    assert result["jobs"][0]["state"] == "running"
    assert o._row("SELECT processed FROM jobs WHERE id = 'j'")["processed"] == 0


async def test_live_logs_are_redacted_and_progress_cannot_ack_a_final_result(tmp_path):
    manager = JobManager(tmp_path)
    sent = []
    manager.notify = sent.append
    job = {"id": "job-1", "state": "running"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import time; print('token=private-value'); time.sleep(.1)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await manager._communicate_progress(process, job)
    assert sent[0]["type"] == "job_progress"
    assert "private-value" not in job["progress"]["stdout_tail"]
    assert "removido" in job["progress"]["stdout_tail"]
    manager.acknowledge(job["id"])
    assert not manager.get(job["id"]).get("acked")


async def test_saved_mobile_request_is_available_to_voice_when_pc_is_offline():
    class Link:
        async def call(self, action, params):
            if action == "orchestrator.progress":
                return {"requests": [{"id": "c", "state": "needs_input"}]}
            raise DesktopLinkError("PC offline")

    result = await RemoteCoding(Link()).report("all", 12)
    assert result["available"] is False
    assert result["orchestrator"]["requests"][0]["state"] == "needs_input"


async def test_progress_crosses_real_websocket_before_job_finishes(orch):  # noqa: F811
    from tests.test_master_phone import _wait

    script = orch["project"].parent / "fake_agent.py"
    script.write_text(
        "import time; time.sleep(5)\n" + script.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    response = await orch["http"].post(
        orch["url"] + "/v1/conversations",
        headers=orch["headers"],
        json={"project": "dicevtt", "text": "pergunta: versao?"},
    )
    conversation = await response.json()
    o = orch["orchestrator"]
    await _wait(
        lambda: any(j["updated_at"] for j in o.conversation(conversation["id"])["jobs"])
    )
    job = o.conversation(conversation["id"])["jobs"][0]
    assert job["state"] in {"preparing", "running"}
    assert job["finished_at"] is None
    assert job["progress"]["stage"]
    await _wait(lambda: o.conversation(conversation["id"])["state"] != "planning")
