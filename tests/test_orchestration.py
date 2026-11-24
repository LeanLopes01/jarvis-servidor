"""Project work through JARVIS (PLANO_ORQUESTRACAO_DICEVTT_JARVIS.md, sections 2-12):
plan, approve a version, run tasks in a work copy, report, apply only on request."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from src.jarvis_control.desktop import DesktopHub
from src.jarvis_control.orchestrator import Orchestrator
from src.jarvis_control.sessions import SessionManager
from src.jarvis_control.store import Store
from src.jarvis_control.web import build_public_app
from src.jarvis_desktop import workspaces
from src.jarvis_desktop.actions import orchestrator_actions
from src.jarvis_desktop.agent_jobs import JobManager, read_result
from src.jarvis_desktop.client import DesktopClient
from tests.test_master_phone import _start, _wait, pair
from tests.test_reliability import FakeWriter

FAKE_AGENT = r"""
import json, re, sys, pathlib
kind, agent, prompt_file = sys.argv[1], sys.argv[2], sys.argv[3]
prompt = pathlib.Path(prompt_file).read_text(encoding="utf-8")
if kind == "coordinate" and agent == "codex" and "UNIT_QUOTA" in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": "t0"}))
    print(json.dumps({"type": "turn.failed", "error": {"message":
        "You've hit your usage limit. Try again at 3:45 PM."}}))
    sys.exit(1)
if kind == "coordinate" and "UNIT_DENY" in prompt:
    print(json.dumps({"conversation_id": "agy-1", "status": "SUCCESS", "response": "",
                      "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}))
elif kind == "coordinate":
    if "Write the final report" in prompt:
        out = {"outcome": "completed", "reply": "Iniciativa ajustada.", "spoken_summary": "The initiative change is ready for review, Sir.",
               "changes": ["Initiative.cs: ordem estável"], "verifications": ["revisão do código"], "pending": ["testar no Unity"]}
    elif "pergunta" in prompt:
        out = {"action": "answer", "reply": "O sistema de iniciativa fica em Initiative.cs.", "spoken_summary": "It lives in the initiative script, Sir.",
               "questions": [], "plan": {"title": "", "goal": "", "summary": "", "tasks": [], "tests": [], "risks": []}}
    else:
        keep_ui = "interface" in prompt
        m = re.search(r'give every task the agent "([^"]+)"', prompt)
        exec_agent = m.group(1) if m else "claude"
        out = {"action": "plan", "reply": "Plano pronto.", "spoken_summary": "I have a plan with two tasks.", "questions": [],
               "plan": {"title": "Iniciativa", "goal": "Ordem estável" + (" sem mudar a interface" if keep_ui else ""), "summary": "s",
                        "tasks": [
                            {"key": "impl", "agent": exec_agent, "title": "Implementar", "objective": "o", "files": ["Assets/Scripts/Initiative.cs"],
                             "acceptance": ["empates estáveis"], "depends_on": []},
                            {"key": "review", "agent": exec_agent, "title": "Revisar", "objective": "o", "files": [],
                             "acceptance": ["sem regressões"], "depends_on": ["impl"]}],
                        "tests": ["leitura"], "risks": []}}
        if "UNIT_PIPELINE" in prompt:
            out['plan']['tasks'][0].update(stage='ui', agent='codex')
            out['plan']['tasks'][1].update(stage='coding', agent='claude')
        if "UNIT_WRONG_AGENT" in prompt:
            out["plan"]["tasks"][1]["agent"] = "antigravity" if exec_agent != "antigravity" else "claude"
    if agent == "codex":
        pathlib.Path(sys.argv[4], "last_message.txt").write_text(json.dumps(out))
        print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}))
    else:
        print(json.dumps({"conversation_id": "agy-1", "status": "SUCCESS", "response": "", "structured_output": out}))
elif agent == "codex" or (agent in {"claude", "antigravity"} and "(impl)" in prompt):
    target = pathlib.Path("Assets/Scripts/Initiative.cs")
    target.write_text(target.read_text() + "\n// stable order\n")
    pathlib.Path(sys.argv[4], "last_message.txt").write_text("Changed Initiative.cs; not compiled.")
    if agent == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    elif agent == "antigravity":
        print(json.dumps({"conversation_id": "agy-exec-1", "status": "SUCCESS", "response": "Changed Initiative.cs; not compiled."}))
    else:
        print(json.dumps({"result": "Changed Initiative.cs; not compiled.", "session_id": "s0", "is_error": False}))
elif agent == "antigravity":
    print(json.dumps({"conversation_id": "agy-exec-2", "status": "SUCCESS", "response": "Reviewed; looks right."}))
else:
    print(json.dumps({"result": "Reviewed; looks right.", "session_id": "s1", "is_error": False}))
"""


@pytest.fixture
async def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project_dir = tmp_path / "DiceVTT"
    (project_dir / "Assets" / "Scripts").mkdir(parents=True)
    (project_dir / "Assets" / "Scripts" / "Initiative.cs").write_text(
        "class Initiative {}\n"
    )
    (project_dir / "Assets" / "Scripts" / "Initiative.cs.meta").write_text("guid: 1\n")
    (project_dir / "Library").mkdir()
    (project_dir / "Library" / "cache.bin").write_bytes(b"x" * 10)
    (project_dir / "ProjectSettings").mkdir()
    (project_dir / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.10f1\n"
    )
    monkeypatch.setattr(
        workspaces,
        "projects",
        lambda: {
            "dicevtt": workspaces.Project("dicevtt", "DiceVTT", project_dir, "none")
        },
    )
    from src.jarvis_desktop import agent_jobs

    monkeypatch.setattr(agent_jobs, "projects", workspaces.projects)
    monkeypatch.setenv("JARVIS_WORK_ROOT", str(tmp_path / "work"))
    agent_script = tmp_path / "fake_agent.py"
    agent_script.write_text(FAKE_AGENT, encoding="utf-8")

    store = Store(":memory:")
    hub = DesktopHub(store)
    writer = FakeWriter()
    orchestrator = Orchestrator(":memory:", store, hub, hub.bus, writer=writer)  # type: ignore[arg-type]
    hub.job_update = orchestrator.record_job_update

    async def unit_state() -> str:
        return "inactive"

    app = build_public_app(
        store=store,
        manager=SessionManager(store, None, None),
        hub=hub,
        rooms=None,  # type: ignore[arg-type]
        unit_state=unit_state,
        maintenance=lambda: False,
        version="test",
        orchestrator=orchestrator,
    )
    runner, port = await _start(app)
    _, token = pair(store, master=True)
    _, desktop_token = pair(store, "desktop")
    jobs = JobManager(tmp_path / "jobs")

    def command_for(job: dict[str, Any], cwd: Path, files: Path) -> list[str]:
        prompt = files / "prompt.txt"
        prompt.write_text(job["prompt"], encoding="utf-8")
        return [
            sys.executable,
            str(agent_script),
            job["kind"],
            job.get("agent") or "-",
            str(prompt),
            str(files),
        ]

    jobs.command_for = command_for
    actions = orchestrator_actions(jobs)
    client = DesktopClient(
        f"http://127.0.0.1:{port}", desktop_token, actions, jobs=jobs
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(client.run_forever(stop))
    await _wait(lambda: bool(hub.status()["capabilities"]))
    async with aiohttp.ClientSession() as http:
        yield {
            "http": http,
            "url": f"http://127.0.0.1:{port}",
            "headers": {"Authorization": f"Bearer {token}"},
            "orchestrator": orchestrator,
            "project": project_dir,
            "writer": writer,
            "store": store,
            "jobs": jobs,
            "actions": actions,  # the helper's handlers, replaceable in a test
        }
    stop.set()
    await asyncio.wait_for(task, 5)
    await runner.cleanup()


async def _conversation(chain: dict[str, Any], cid: str) -> dict[str, Any]:
    response = await chain["http"].get(
        f"{chain['url']}/v1/conversations/{cid}", headers=chain["headers"]
    )
    return await response.json()


async def _until(
    chain: dict[str, Any], cid: str, predicate, timeout: float = 30
) -> dict[str, Any]:
    for _ in range(int(timeout / 0.1)):
        conv = await _conversation(chain, cid)
        if predicate(conv):
            return conv
        await asyncio.sleep(0.1)
    raise AssertionError(f"conversation state: {conv['state']}")


async def test_plan_approve_run_report_and_apply(orch) -> None:
    http, url, headers = orch["http"], orch["url"], orch["headers"]
    created = await http.post(
        f"{url}/v1/conversations",
        json={
            "project": "dicevtt",
            "text": "Quero melhorar a iniciativa",
            "request_id": "r1",
        },
        headers=headers,
    )
    assert created.status == 202
    cid = (await created.json())["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "awaiting_approval")
    [plan] = conv["plans"]
    assert plan["version"] == 1 and plan["state"] == "proposed"
    original = (orch["project"] / "Assets/Scripts/Initiative.cs").read_text()

    # A change request replaces the plan with a new version; the old one cannot run.
    await http.post(
        f"{url}/v1/conversations/{cid}/messages",
        json={"text": "Aprove, mas mantenha a interface atual"},
        headers=headers,
    )
    conv = await _until(
        orch, cid, lambda c: len(c["plans"]) == 2 and c["state"] == "awaiting_approval"
    )
    old, new = conv["plans"]
    assert old["state"] == "superseded" and "interface" in new["body"]["goal"]
    stale = await http.post(
        f"{url}/v1/plans/{old['id']}/approve", json={"version": 1}, headers=headers
    )
    assert stale.status == 409
    wrong = await http.post(
        f"{url}/v1/plans/{new['id']}/approve", json={"version": 1}, headers=headers
    )
    assert wrong.status == 409
    approved = await (
        await http.post(
            f"{url}/v1/plans/{new['id']}/approve",
            json={"version": 2, "hash": new["hash"]},
            headers=headers,
        )
    ).json()
    run_id = approved["run_id"]

    conv = await _until(
        orch,
        cid,
        lambda c: c["runs"] and c["runs"][0]["state"] not in {"running", "reviewing"},
    )
    [run] = conv["runs"]
    assert run["state"] == "completed"
    assert [t["state"] for t in run["tasks"]] == ["completed", "completed"]
    impl = run["tasks"][0]["result"]
    assert impl["changes"]["modified"] == ["Assets/Scripts/Initiative.cs"]
    assert "+// stable order" in impl["changes"]["diff"]
    assert run["report"]["pending"] == ["testar no Unity"]
    # Nothing touched the real project before the explicit apply.
    assert (orch["project"] / "Assets/Scripts/Initiative.cs").read_text() == original
    events = [e["kind"] for e in orch["store"].events_after(0)]
    assert events.count("plan_ready") == 2 and events[-1] == "run_finished"
    working = [
        e["summary"]
        for e in orch["store"].events_after(0)
        if e["kind"] == "project_working"
    ]
    # A client without routing gets Codex to plan and Claude to execute.
    assert "Claude Code is starting task 1 of 2, Sir." in working
    assert "Claude Code is starting task 2 of 2, Sir." in working
    assert working[-1] == "The tasks are done. Codex is writing the report, Sir."
    assert {j.get("agent") for j in orch["jobs"]._all()} == {"codex", "claude"}

    applied = await (
        await http.post(f"{url}/v1/runs/{run_id}/apply", headers=headers)
    ).json()
    assert (
        applied["applied"] == ["Assets/Scripts/Initiative.cs"]
        and applied["conflicts"] == []
    )
    assert (
        "stable order" in (orch["project"] / "Assets/Scripts/Initiative.cs").read_text()
    )

    await orch["orchestrator"].export()
    files = orch["writer"].files
    assert f"DiceVTT/conversas/{cid}/conversa.md" in files
    assert f"DiceVTT/conversas/{cid}/planos/002.md" in files
    assert f"DiceVTT/conversas/{cid}/relatorios/{run_id}.md" in files


async def test_a_question_gets_an_answer_without_a_plan(orch) -> None:
    http, url, headers = orch["http"], orch["url"], orch["headers"]
    cid = (
        await (
            await http.post(
                f"{url}/v1/conversations",
                json={"text": "Uma pergunta: onde fica a iniciativa?"},
                headers=headers,
            )
        ).json()
    )["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "idle")
    assert conv["plans"] == []
    assert conv["messages"][-1]["author"] == "codex"
    assert [j["agent"] for j in conv["jobs"]] == ["codex"]
    # The master phone hears that work started, then the answer itself.
    events = [(e["kind"], e["summary"]) for e in orch["store"].events_after(0)]
    assert events[0][0] == "project_working"
    assert events[-1] == ("project_reply", "It lives in the initiative script, Sir.")


async def test_conflicting_file_is_not_applied(tmp_path: Path) -> None:
    project_dir = tmp_path / "P"
    (project_dir / "Assets").mkdir(parents=True)
    (project_dir / "Assets" / "a.cs").write_text("one\n")
    project = workspaces.Project("p", "P", project_dir, "none")
    work = tmp_path / "work"
    workspaces.copy_project(project, work)
    workspaces.keep_reference(work)
    (work / "Assets" / "a.cs").write_text("agent\n")
    (work / "Assets" / "b.cs").write_text("new\n")
    (project_dir / "Assets" / "a.cs").write_text("the user edited it meanwhile\n")
    result = workspaces.apply_to_project(project, work, workspaces.load_manifest(work))
    assert result["conflicts"] == ["Assets/a.cs"] and result["applied"] == [
        "Assets/b.cs"
    ]
    assert (
        project_dir / "Assets" / "a.cs"
    ).read_text() == "the user edited it meanwhile\n"


def test_a_refused_tool_is_not_success() -> None:
    job = {"kind": "coordinate", "exit_code": 0}
    stdout = json.dumps(
        {
            "conversation_id": "x",
            "status": "SUCCESS",
            "response": "",
            "denied_actions": [{"action": "command"}],
        }
    )
    result = read_result(job, stdout, Path("."))
    assert result["ok"] is False and "refused" in result["problem"]


def test_plans_with_paths_outside_the_project_are_refused() -> None:
    problems = Orchestrator._check_plan(
        {
            "tasks": [
                {
                    "key": "a",
                    "agent": "codex",
                    "files": ["../secrets.txt"],
                    "depends_on": [],
                },
                {"key": "b", "agent": "hal9000", "files": [], "depends_on": ["zzz"]},
            ]
        }
    )
    assert len(problems) == 3


async def test_cancel_a_running_run(orch, monkeypatch) -> None:
    http, url, headers, jobs = orch["http"], orch["url"], orch["headers"], orch["jobs"]
    cid = (
        await (
            await http.post(
                f"{url}/v1/conversations",
                json={"text": "Quero melhorar a iniciativa"},
                headers=headers,
            )
        ).json()
    )["id"]
    conv = await _until(orch, cid, lambda c: c["state"] == "awaiting_approval")
    slow = jobs.command_for

    def hang(job, cwd, files):
        if job["kind"] == "execute":
            return [sys.executable, "-c", "import time; time.sleep(60)"]
        return slow(job, cwd, files)

    jobs.command_for = hang
    plan = conv["plans"][0]
    run_id = (
        await (
            await http.post(
                f"{url}/v1/plans/{plan['id']}/approve", json={}, headers=headers
            )
        ).json()
    )["run_id"]
    await _until(orch, cid, lambda c: c["runs"][0]["tasks"][0]["state"] == "running")
    await asyncio.sleep(1)  # the process is running on the "PC"
    await http.post(f"{url}/v1/runs/{run_id}/cancel", headers=headers)
    conv = await _until(orch, cid, lambda c: c["runs"][0]["state"] == "cancelled")
    states = [t["state"] for t in conv["runs"][0]["tasks"]]
    assert states == ["cancelled", "cancelled"]


async def test_a_refused_command_gets_one_more_turn(orch) -> None:
    http, url, headers = orch["http"], orch["url"], orch["headers"]
    created = await http.post(
        f"{url}/v1/conversations",
        json={
            "text": "UNIT_DENY melhorar a iniciativa",
            "routing_version": 1,
            "planning_agent": "antigravity",
            "execution_agent": "claude",
        },
        headers=headers,
    )
    cid = (await created.json())["id"]
    conv = await _until(
        orch, cid, lambda c: c["state"] in {"awaiting_approval", "failed"}
    )
    assert conv["state"] == "awaiting_approval"
    [job] = [j for j in orch["jobs"]._all() if j["kind"] == "coordinate"]
    assert job["result"]["first_attempt_denied"][0]["action"] == "command"


async def test_a_long_request_says_it_is_still_working() -> None:
    now = [1000.0]
    store = Store(":memory:")
    hub = DesktopHub(store)
    orchestrator = Orchestrator(":memory:", store, hub, hub.bus, clock=lambda: now[0])
    orchestrator._db.execute(
        "INSERT INTO conversations (id, project, device_id, title, state, created_at, updated_at)"
        " VALUES ('c1', 'dicevtt', 'd', 't', 'planning', 0, 0)"
    )
    orchestrator._still_working()  # first sight (e.g. after a restart): no reminder
    now[0] += 179
    orchestrator._still_working()
    now[0] += 2
    orchestrator._still_working()
    summaries = [e["summary"] for e in store.events_after(0)]
    assert summaries == ["Still working on it, Sir: 3 minutes so far."]
    orchestrator._db.execute("UPDATE conversations SET state = 'idle'")
    now[0] += 600
    orchestrator._still_working()
    assert len(store.events_after(0)) == 1
