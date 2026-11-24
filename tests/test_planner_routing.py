"""Planner routing (docs/claude-selecao-agente-preliminar.md in the Orquestrador):
the request chooses Codex or Antigravity to plan, Claude executes after approval,
and every choice stays with its own request, plan, run and job."""

import json
from pathlib import Path

import pytest

from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.orchestrator import Orchestrator, OrchestratorError
from src.jarvis_control.store import Store
from src.jarvis_desktop import orchestrator_runtime
from src.jarvis_desktop.agent_jobs import command_for, quota_info, read_result
from tests.test_orchestration import _until, orch  # noqa: F401

CODEX = {"routing_version": 1, "planning_agent": "codex", "execution_agent": "claude"}
AGY = {**CODEX, "planning_agent": "antigravity"}


async def _post(chain, path: str, body: dict):
    response = await chain["http"].post(
        chain["url"] + path, json=body, headers=chain["headers"]
    )
    return response.status, await response.json()


async def _approve_and_finish(chain, cid: str, conv: dict) -> dict:
    plan = conv["plans"][-1]
    status, _ = await _post(
        chain,
        f"/v1/plans/{plan['id']}/approve",
        {"version": plan["version"], "hash": plan["hash"]},
    )
    assert status == 200
    return await _until(
        chain,
        cid,
        lambda c: c["runs"] and c["runs"][-1]["state"] not in {"running", "reviewing"},
    )


async def test_runtime_announces_routing_only_with_a_routing_helper(orch) -> None:  # noqa: F811
    response = await orch["http"].get(
        orch["url"] + "/v1/orchestration/runtime", headers=orch["headers"]
    )
    runtime = await response.json()
    assert runtime["planning_routing"] == {
        "version": 1,
        "agents": ["codex", "antigravity"],
        "execution_agent": "claude",
        "execution_agents": ["claude", "antigravity", "codex"],
        "pipeline_version": 1,
    }
    assert "planning_agents" not in runtime  # the helper's raw fields stay internal
    assert "execution_agents" not in runtime

    async def old_helper(params=None):
        return {"online": True, "host": "BATCOMPUTADOR"}

    orch["actions"]["orchestrator.runtime"] = old_helper
    response = await orch["http"].get(
        orch["url"] + "/v1/orchestration/runtime", headers=orch["headers"]
    )
    assert "planning_routing" not in await response.json()
    # Neither an explicit choice nor the default reaches a helper that would
    # silently plan with Antigravity.
    for body in ({"text": "Quero algo", **CODEX}, {"text": "Quero algo"}):
        status, error = await _post(orch, "/v1/conversations", body)
        assert (status, error["error"]) == (503, "planner_unavailable")
    assert orch["orchestrator"].conversations() == []
    assert orch["jobs"]._all() == []


async def test_codex_plans_claude_executes_and_codex_reports(orch) -> None:  # noqa: F811
    status, conv = await _post(
        orch,
        "/v1/conversations",
        {"text": "Quero melhorar a iniciativa", "request_id": "c1", **CODEX},
    )
    assert status == 202
    cid = conv["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "awaiting_approval")
    [plan] = conv["plans"]
    assert (plan["planning_agent"], plan["execution_agent"]) == ("codex", "claude")
    assert {t["agent"] for t in plan["body"]["tasks"]} == {"claude"}
    assert conv["messages"][0]["data"] == {
        "routing": {"planning_agent": "codex", "execution_agent": "claude"}
    }
    [planner] = orch["jobs"]._all()
    assert (planner["kind"], planner["agent"]) == ("coordinate", "codex")
    assert planner["result"]["usage"]["input_tokens"] == 10

    # A continuation keeps planning with Codex.
    status, _ = await _post(
        orch,
        f"/v1/conversations/{cid}/messages",
        {"text": "mantenha a interface", **CODEX},
    )
    assert status == 202
    conv = await _until(
        orch, cid, lambda c: len(c["plans"]) == 2 and c["state"] == "awaiting_approval"
    )
    conv = await _approve_and_finish(orch, cid, conv)
    run = conv["runs"][0]
    assert run["state"] == "completed"
    assert (run["planning_agent"], run["execution_agent"], run["report_agent"]) == (
        "codex",
        "claude",
        "codex",
    )
    jobs = orch["jobs"]._all()
    # No Antigravity process anywhere in this flow, report included.
    assert {(j["kind"], j["agent"]) for j in jobs} == {
        ("coordinate", "codex"),
        ("execute", "claude"),
    }
    assert conv["messages"][-1]["author"] == "codex"
    assert {j["agent"] for j in conv["jobs"]} == {"codex", "claude"}


async def test_antigravity_choice_uses_antigravity(orch) -> None:  # noqa: F811
    _, conv = await _post(orch, "/v1/conversations", {"text": "Quero melhorar", **AGY})
    cid = conv["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "awaiting_approval")
    assert conv["plans"][0]["planning_agent"] == "antigravity"
    assert conv["coordinator_id"] == "agy-1"
    conv = await _approve_and_finish(orch, cid, conv)
    assert conv["runs"][0]["report_agent"] == "antigravity"
    assert {(j["kind"], j["agent"]) for j in orch["jobs"]._all()} == {
        ("coordinate", "antigravity"),
        ("execute", "claude"),
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({**CODEX, "planning_agent": "grok"}, "agent_unavailable"),
        ({**CODEX, "planning_agent": "hal9000"}, "unknown_agent"),
        ({**CODEX, "execution_agent": "grok"}, "agent_unavailable"),
        ({**CODEX, "execution_agent": "hal9000"}, "unsupported_executor"),
        ({**CODEX, "routing_version": 2}, "unsupported_routing"),
        ({**CODEX, "routing_version": "1"}, "unsupported_routing"),
        ({"planning_agent": "codex"}, "unsupported_routing"),
    ],
)
async def test_invalid_routing_is_refused_before_anything_runs(
    orch,  # noqa: F811
    body,
    code,
) -> None:
    status, error = await _post(orch, "/v1/conversations", {"text": "x", **body})
    assert (status, error["error"]) == (400, code)
    assert orch["orchestrator"].conversations() == []
    assert orch["jobs"]._all() == []


async def test_same_request_returns_the_same_request_and_a_changed_one_conflicts(
    orch,  # noqa: F811
) -> None:
    body = {"text": "Quero melhorar", "request_id": "same", **CODEX}
    _, first = await _post(orch, "/v1/conversations", body)
    status, again = await _post(orch, "/v1/conversations", body)
    assert status == 202 and again["id"] == first["id"]
    for changed in ({**body, **AGY}, {**body, "text": "Outra coisa"}):
        status, error = await _post(orch, "/v1/conversations", changed)
        assert (status, error["error"]) == (409, "request_conflict")
    conv = await _until(orch, first["id"], lambda c: c["state"] != "planning")
    assert len([m for m in conv["messages"] if m["role"] == "user"]) == 1
    assert len(orch["jobs"]._all()) == 1


async def test_simultaneous_requests_keep_their_own_planner(orch) -> None:  # noqa: F811
    _, a = await _post(orch, "/v1/conversations", {"text": "pergunta: a?", **CODEX})
    _, b = await _post(orch, "/v1/conversations", {"text": "pergunta: b?", **AGY})
    for cid in (a["id"], b["id"]):
        await _until(orch, cid, lambda c: c["state"] == "idle")
    agents = {
        j["conversation_id"]: j["agent"]
        for j in orch["jobs"]._all()
        if j["kind"] == "coordinate"
    }
    assert agents == {a["id"]: "codex", b["id"]: "antigravity"}


async def test_exhausted_quota_fails_once_and_resumes_only_on_request(orch) -> None:  # noqa: F811
    _, conv = await _post(
        orch, "/v1/conversations", {"text": "UNIT_QUOTA mudar", **CODEX}
    )
    cid = conv["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "failed")
    [job] = orch["jobs"]._all()
    assert job["agent"] == "codex" and job["result"]["quota_exhausted"] is True
    assert job["result"]["retry_at"] == "3:45 PM"
    assert "liberação informada: 3:45 PM" in conv["messages"][-1]["text"]
    await orch["orchestrator"].tick()  # nothing is repeated in the background
    assert len(orch["jobs"]._all()) == 1
    # Resuming is an explicit new message, which plans again.
    await _post(
        orch,
        f"/v1/conversations/{cid}/messages",
        {"text": "pergunta: e agora?", **CODEX},
    )
    await _until(orch, cid, lambda c: c["state"] != "planning")
    assert len(orch["jobs"]._all()) == 2


async def test_a_plan_for_another_executor_is_refused_not_rewritten(orch) -> None:  # noqa: F811
    _, conv = await _post(
        orch, "/v1/conversations", {"text": "UNIT_WRONG_AGENT mudar", **CODEX}
    )
    conv = await _until(orch, conv["id"], lambda c: c["state"] == "failed")
    assert conv["plans"] == []
    assert "review is for antigravity" in conv["messages"][-1]["text"]


async def test_history_without_routing_keeps_its_origin(orch) -> None:  # noqa: F811
    o = orch["orchestrator"]
    body = {
        "title": "Antigo",
        "goal": "g",
        "summary": "s",
        "tasks": [
            {
                "key": "impl",
                "agent": "codex",
                "title": "Implementar",
                "objective": "o",
                "files": ["Assets/Scripts/Initiative.cs"],
                "acceptance": [],
                "depends_on": [],
            },
        ],
        "tests": [],
        "risks": [],
    }
    o._db.execute(
        "INSERT INTO conversations (id, project, device_id, title, state, created_at, updated_at)"
        " VALUES ('old', 'dicevtt', 'd', 'Antigo', 'awaiting_approval', 1, 1)"
    )
    o._db.execute(
        "INSERT INTO plans (id, conversation_id, version, hash, state, body, created_at)"
        " VALUES ('p-old', 'old', 1, 'h', 'proposed', ?, 1)",
        (json.dumps(body),),
    )
    conv = await _approve_and_finish(orch, "old", await _conv(orch, "old"))
    run = conv["runs"][0]
    assert run["planning_agent"] is None and run["report_agent"] == "antigravity"
    assert run["tasks"][0]["agent"] == "codex"
    assert conv["plans"][0]["planning_agent"] is None


async def _conv(chain, cid):
    response = await chain["http"].get(
        f"{chain['url']}/v1/conversations/{cid}", headers=chain["headers"]
    )
    return await response.json()


async def test_offline_server_without_runtime_check_refuses_nothing_silently() -> None:
    store = Store(":memory:")
    hub = DesktopHub(store)
    orchestrator = Orchestrator(":memory:", store, hub, hub.bus)
    with pytest.raises(OrchestratorError) as exc:
        await orchestrator.post_message(
            "d", "dicevtt", "x", routing={**CODEX, "planning_agent": "grok"}
        )
    assert exc.value.code == "agent_unavailable"
    # With the PC offline (and no local runtime required) the job waits for it,
    # and is checked against the helper again when it is sent.
    conv = await orchestrator.post_message("d", "dicevtt", "x")
    for task in list(orchestrator._tasks):
        await task
    [job] = orchestrator._rows("SELECT * FROM jobs")
    assert job["state"] == "waiting_for_pc"
    assert json.loads(job["params"])["agent"] == "codex"
    assert conv["messages"][0]["data"]["routing"]["planning_agent"] == "codex"


def test_codex_planner_runs_read_only_with_the_schema(
    tmp_path: Path, monkeypatch
) -> None:
    from src.jarvis_desktop import agent_jobs

    monkeypatch.setattr(agent_jobs, "_exe", lambda name: name)
    job = {
        "kind": "coordinate",
        "agent": "codex",
        "prompt": "p",
        "schema": {"type": "object"},
        "timeout": 60,
    }
    args = command_for(job, tmp_path / "ref", tmp_path)
    assert args[:2] == ["codex", "exec"]
    assert args[args.index("-s") + 1] == "read-only"
    assert args[args.index("--output-schema") + 1] == str(tmp_path / "schema.json")
    assert "workspace-write" not in args and args[-1] == "-"
    assert agent_jobs.stdin_for(job) == b"p"
    # Legacy coordinate jobs (no agent) stay Antigravity.
    assert command_for({**job, "agent": None}, tmp_path, tmp_path)[0] == "agy"


def test_codex_plan_needs_valid_structured_output(tmp_path: Path) -> None:
    job = {"kind": "coordinate", "agent": "codex", "exit_code": 0}
    (tmp_path / "last_message.txt").write_text("not json")
    result = read_result(job, "", tmp_path)
    assert result["ok"] is False and "estruturado" in result["problem"]
    (tmp_path / "last_message.txt").write_text(json.dumps({"action": "answer"}))
    assert read_result(job, "", tmp_path)["ok"] is True
    assert read_result({**job, "exit_code": 1}, "", tmp_path)["ok"] is False


def test_quota_messages() -> None:
    info = quota_info(["", "ERROR: rate limit reached, try again in 2 hours."])
    assert info and info["retry_at"] == "2 hours"
    assert quota_info(["Login required"]) is None
    assert quota_info([None, "RESOURCE_EXHAUSTED"])["retry_at"] is None


async def test_helper_runtime_always_reports_its_planners() -> None:
    status = await orchestrator_runtime.runtime_status()
    assert status["routing_version"] == 1
    assert status["planning_agents"] == ["codex", "antigravity"]
    assert status["execution_agents"] == ["claude", "antigravity", "codex"]


async def test_antigravity_and_codex_can_execute_approved_plans(orch) -> None:  # noqa: F811
    route = {"routing_version": 1, "planning_agent": "codex", "execution_agent": "antigravity"}
    status, conv = await _post(
        orch,
        "/v1/conversations",
        {"text": "Quero melhorar a iniciativa", "request_id": "agy-exec", **route},
    )
    assert status == 202
    cid = conv["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "awaiting_approval")
    [plan] = conv["plans"]
    assert (plan["planning_agent"], plan["execution_agent"]) == ("codex", "antigravity")
    assert {t["agent"] for t in plan["body"]["tasks"]} == {"antigravity"}
    conv = await _approve_and_finish(orch, cid, conv)
    run = conv["runs"][0]
    assert run["state"] == "completed"
    assert (run["planning_agent"], run["execution_agent"], run["report_agent"]) == (
        "codex",
        "antigravity",
        "codex",
    )
    assert {(j["kind"], j["agent"]) for j in orch["jobs"]._all()} == {
        ("coordinate", "codex"),
        ("execute", "antigravity"),
    }


async def test_old_routing_helper_falls_back_to_claude_only_executor(orch) -> None:  # noqa: F811
    async def claude_only_helper(params=None):
        return {
            "online": True,
            "host": "BATCOMPUTADOR",
            "routing_version": 1,
            "planning_agents": ["codex", "antigravity"],
        }

    orch["actions"]["orchestrator.runtime"] = claude_only_helper
    response = await orch["http"].get(
        orch["url"] + "/v1/orchestration/runtime", headers=orch["headers"]
    )
    runtime = await response.json()
    assert runtime["planning_routing"]["execution_agents"] == ["claude"]
    status, error = await _post(
        orch,
        "/v1/conversations",
        {
            "text": "Quero algo",
            "routing_version": 1,
            "planning_agent": "codex",
            "execution_agent": "antigravity",
        },
    )
    assert (status, error["error"]) == (503, "executor_unavailable")


def test_antigravity_executor_includes_add_dir_and_conversation(
    tmp_path: Path, monkeypatch
) -> None:
    from src.jarvis_desktop import agent_jobs

    if not hasattr(agent_jobs, "EXECUTORS"):
        pytest.skip("PC helper agent_jobs overlay")
    monkeypatch.setattr(agent_jobs, "_exe", lambda name: name)
    work = tmp_path / "work"
    args = command_for(
        {
            "kind": "execute",
            "agent": "antigravity",
            "prompt": "edit file",
            "timeout": 120,
            "agy_conversation_id": "conv-99",
        },
        work,
        tmp_path,
    )
    assert args[args.index("--mode") + 1] == "accept-edits"
    assert args[args.index("--add-dir") + 1] == str(work)
    assert args[args.index("--conversation") + 1] == "conv-99"
