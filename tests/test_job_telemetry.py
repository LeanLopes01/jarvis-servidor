"""Live telemetry of the coding CLIs and the job lifecycle around it.

The stream samples are trimmed copies of real output from the installed versions
(Claude Code 2.1.303, Codex 0.163.0, Antigravity 1.4.5), captured on 22/10/2026.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from src.jarvis_control.orchestrator import progress_body
from src.jarvis_desktop import agent_jobs
from src.jarvis_desktop.agent_jobs import (
    JobManager,
    active_jobs,
    read_result,
    stdin_for,
)
from src.jarvis_desktop.job_stream import StreamTelemetry, final_line

CLAUDE = [
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {
        "type": "system",
        "subtype": "init",
        "model": "claude-opus-5-5",
        "session_id": "s",
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-5-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Glob",
                    "input": {"pattern": "*"},
                }
            ],
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 50,
                "output_tokens": 10,
            },
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "tool_use_id": "t1",
                    "type": "tool_result",
                    "content": "No files found",
                }
            ]
        },
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_2",
            "content": [
                {"type": "thinking", "thinking": "SEGREDO-INTERNO", "signature": "x"}
            ],
            "usage": {"input_tokens": 2, "output_tokens": 5},
        },
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_2",
            "content": [{"type": "text", "text": "A pasta está vazia."}],
            "usage": {"input_tokens": 2, "output_tokens": 5},
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "A pasta está vazia.",
        "session_id": "s",
        "total_cost_usd": 0.37,
        "permission_denials": [],
        "usage": {
            "input_tokens": 4,
            "cache_creation_input_tokens": 44870,
            "cache_read_input_tokens": 41922,
            "output_tokens": 106,
        },
    },
]
CODEX = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "agent_message",
            "text": "Vou verificar os arquivos.",
        },
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "pwsh -Command Get-ChildItem",
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "pwsh -Command Get-ChildItem",
            "exit_code": 0,
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "reasoning",
            "text": "Conferir a pasta primeiro.",
        },
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 71404,
            "cached_input_tokens": 61056,
            "output_tokens": 174,
        },
    },
]
AGY = [
    {"event": "init", "conversation_id": "c1", "init": {"cwd": "."}},
    {
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "agent_response",
            "text_delta": "Olá!",
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": "\n",
            "usage": {"input_tokens": 7618, "output_tokens": 1498},
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "step_index": 2,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {"parameters": {"CommandLine": "Get-ChildItem -Force"}},
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "step_index": 3,
            "state": "ERROR",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {
                "parameters": {"CommandLine": "Get-Location"},
                "error": {"message": "user denied permission"},
            },
        },
    },
    {
        "event": "result",
        "result": {
            "conversation_id": "c1",
            "status": "SUCCESS",
            "response": "Olá!",
            "structured_output": {"reply": "Olá!"},
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            "usage": {"input_tokens": 17069, "output_tokens": 1801},
        },
    },
]


def _stream(lines: list[dict]) -> bytes:
    return "".join(
        json.dumps(line, ensure_ascii=False) + "\n" for line in lines
    ).encode()


def _feed(agent: str, lines: list[dict]) -> StreamTelemetry:
    telemetry = StreamTelemetry("job", agent, clock=lambda: 1000.0)
    data = _stream(lines)
    for start in range(0, len(data), 37):  # lines split across reads, as from a pipe
        telemetry.feed(data[start : start + 37])
    return telemetry


def test_claude_stream_shows_tools_and_messages_but_never_thinking() -> None:
    snap = _feed("claude", CLAUDE).snapshot(now=1000.0)
    texts = [e["text"] for e in snap["events"]]
    assert "Glob: *" in texts and "A pasta está vazia." in texts
    assert "SEGREDO" not in json.dumps(snap)
    assert [e["id"] for e in snap["events"]] == [
        f"job:{i}" for i in range(1, len(texts) + 1)
    ]
    # Final usage from the result: input includes the cache, never the cost estimate.
    assert snap["usage"] == {
        "model": "claude-opus-5-5",
        "input_tokens": 86796,
        "output_tokens": 106,
        "source": "claude-cli",
    }


def test_claude_usage_counts_each_message_once_while_running() -> None:
    snap = _feed("claude", CLAUDE[:6]).snapshot(now=1000.0)
    assert (
        snap["usage"]["input_tokens"] == 152 + 2
        and snap["usage"]["output_tokens"] == 15
    )


def test_codex_stream_has_commands_reasoning_summary_and_usage() -> None:
    snap = _feed("codex", CODEX).snapshot(now=1000.0)
    texts = [e["text"] for e in snap["events"]]
    assert "Comando: pwsh -Command Get-ChildItem" in texts
    assert snap["reasoning_summary"] == "Conferir a pasta primeiro."
    assert snap["usage"] == {
        "input_tokens": 71404,
        "output_tokens": 174,
        "source": "codex-cli",
    }


def test_antigravity_stream_reports_tools_denials_and_final_usage() -> None:
    telemetry = _feed("antigravity", AGY)
    snap = telemetry.snapshot(now=1000.0)
    texts = [e["text"] for e in snap["events"]]
    assert "run_command: Get-ChildItem -Force" in texts
    assert any("Permissão negada" in t and "RunCommand" in t for t in texts)
    assert snap["usage"]["input_tokens"] == 17069
    # The exact refused command is kept for the job's diagnosis.
    assert telemetry.denied == ["run_command: Get-Location"]


def test_silence_is_reported_without_inventing_activity() -> None:
    telemetry = StreamTelemetry("job", "claude", clock=lambda: 1000.0)
    assert telemetry.snapshot(now=1000.0) == {
        "events": [],
        "activity": "Processo iniciado; aguardando o primeiro evento do CLI",
    }
    telemetry.feed(_stream(CLAUDE[:3]))
    later = telemetry.snapshot(now=1090.0)
    assert (
        "sem novos eventos há 90 s" in later["activity"]
        and later["last_output_at"] == 1000.0
    )
    assert "reasoning_summary" not in later  # unknown, so absent


def test_events_are_bounded_and_redacted() -> None:
    telemetry = StreamTelemetry("job", "codex", clock=lambda: 1.0)
    message = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "token=abc123secret " + "x" * 5000},
    }
    telemetry.feed(_stream([message] * 150))
    snap = telemetry.snapshot(now=1.0)
    assert len(snap["events"]) == 100 and snap["events"][-1]["id"] == "job:150"
    assert all(len(e["text"]) <= 2000 for e in snap["events"])
    assert "abc123secret" not in json.dumps(snap)


def test_read_result_understands_the_stream_formats() -> None:
    coordinate = read_result(
        {"kind": "coordinate", "exit_code": 0}, _stream(AGY).decode(), Path(".")
    )
    assert coordinate["structured"] == {"reply": "Olá!"} and coordinate["denied"]
    claude = read_result(
        {"kind": "execute", "agent": "claude", "exit_code": 0},
        _stream(CLAUDE).decode(),
        Path("."),
    )
    assert (
        claude["ok"]
        and claude["text"] == "A pasta está vazia."
        and claude["session_id"] == "s"
    )
    assert final_line("not json\n") == {}


def test_prompt_goes_through_stdin_for_claude_and_codex(tmp_path, monkeypatch) -> None:
    job = {
        "kind": "execute",
        "agent": "claude",
        "prompt": "linha 1\nlinha 2 & | %PATH%",
        "timeout": 60,
    }
    assert stdin_for(job) == job["prompt"].encode()
    assert stdin_for({**job, "agent": "antigravity"}) is None
    monkeypatch.setattr(agent_jobs.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    args = agent_jobs.command_for(job, tmp_path, tmp_path)
    assert job["prompt"] not in args and args[1:5] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    codex = agent_jobs.command_for({**job, "agent": "codex"}, tmp_path, tmp_path)
    assert codex[-1] == "-" and job["prompt"] not in codex


def test_claude_gets_only_reading_bash_and_is_told_so(tmp_path, monkeypatch) -> None:
    job = {"kind": "execute", "agent": "claude", "prompt": "x", "timeout": 60}
    monkeypatch.setattr(agent_jobs.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(agent_jobs, "UNITY_EDITORS", tmp_path)
    args = agent_jobs.command_for(job, tmp_path, tmp_path)
    tools = args[args.index("--allowedTools") + 1 :]
    assert "Bash(git status:*)" in tools and "Read" in tools
    assert not any(t in {"Bash", "PowerShell"} for t in tools)
    policy = args[args.index("--append-system-prompt") + 1]
    assert "nobody can approve" in policy and str(tmp_path) in policy
    monkeypatch.setattr(agent_jobs, "UNITY_EDITORS", tmp_path / "missing")
    args = agent_jobs.command_for(job, tmp_path, tmp_path)
    assert "Unity" not in args[args.index("--append-system-prompt") + 1]


def test_codex_cmd_shim_resolves_to_the_native_binary(tmp_path, monkeypatch) -> None:
    shim = tmp_path / "codex.CMD"
    shim.write_text("@echo off")
    native = (
        tmp_path
        / "node_modules/@openai/codex/node_modules"
        / agent_jobs.CODEX_NATIVE[0]
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    monkeypatch.setattr(agent_jobs.shutil, "which", lambda name: str(shim))
    assert agent_jobs._exe("codex") == str(native)
    with pytest.raises(agent_jobs.WorkspaceError):
        agent_jobs._exe("claude")  # a .cmd for anything else is refused


async def test_multiline_prompt_reaches_the_process_intact(tmp_path) -> None:
    manager = JobManager(tmp_path)
    prompt = 'primeira linha\nsegunda & | ^ %X% "aspas"\nação'
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=agent_jobs.NO_WINDOW,
    )
    out, _ = await manager._communicate_progress(
        process, {"id": "j", "state": "running"}, None, prompt.encode()
    )
    assert out.decode() == prompt


async def test_a_restart_after_the_cli_exited_recovers_the_result(
    tmp_path, monkeypatch
) -> None:
    jobs = tmp_path / "jobs"
    files = jobs / "job1"
    files.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    job = {
        "id": "job1",
        "kind": "execute",
        "agent": "claude",
        "state": "running",
        "cwd": str(work),
        "created_at": 1.0,
        "prompt": "p",
        "timeout": 60,
    }
    (jobs / "job1.json").write_text(json.dumps(job))
    (files / "stdout.txt").write_text(_stream(CLAUDE).decode(), encoding="utf-8")
    (files / "stderr.txt").write_text("", encoding="utf-8")
    (files / "exit.json").write_text(json.dumps({"exit_code": 0, "attempt": 0}))
    monkeypatch.setattr(agent_jobs, "load_manifest", lambda path: {})
    monkeypatch.setattr(
        agent_jobs, "changes", lambda path, manifest: {"modified": ["A.cs"]}
    )
    sent = []
    manager = JobManager(jobs, notify=sent.append)
    await asyncio.wait_for(manager._tasks["job1"], 10)
    saved = manager.get("job1")
    assert saved["state"] == "completed" and saved["result"]["changes"] == {
        "modified": ["A.cs"]
    }
    assert "recovered" in saved["result"] and sent[-1]["type"] == "job_update"


def test_a_restart_before_the_cli_exited_is_unknown_including_preparing(
    tmp_path,
) -> None:
    for state in ("running", "preparing"):
        (tmp_path / f"{state}.json").write_text(
            json.dumps({"id": state, "kind": "execute", "state": state})
        )
    manager = JobManager(tmp_path)
    assert (
        manager.get("running")["state"] == "unknown"
        and manager.get("preparing")["state"] == "unknown"
    )


def test_active_jobs_lists_what_stop_would_discard(tmp_path) -> None:
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "id": "aaaaaaaa1",
                "kind": "execute",
                "agent": "codex",
                "state": "running",
                "progress": {"activity": "Comando: dotnet build"},
            }
        )
    )
    (tmp_path / "b.json").write_text(
        json.dumps({"id": "bbbbbbbb1", "kind": "execute", "state": "completed"})
    )
    assert active_jobs(tmp_path) == ["aaaaaaaa execute Codex: Comando: dotnet build"]


def test_server_keeps_only_valid_bounded_telemetry() -> None:
    body = progress_body(
        {
            "stage": "running",
            "activity": "a" * 900,
            "stdout_tail": "out",
            "reasoning_summary": 12,  # not text: dropped, not stringified
            "last_output_at": 1792670029,
            "events": [
                {"id": "j:1", "at": 1.0, "kind": "tool", "text": "Read: A.cs"},
                {"id": "j:2", "at": 2.0, "kind": "weird", "text": "x"},
                {"id": "", "text": "no id"},
                "garbage",
            ],
            "usage": {
                "model": "m",
                "input_tokens": 10,
                "output_tokens": -1,
                "cost_usd": float("nan"),
                "source": "codex-cli",
            },
            "private": "dropped",
        }
    )
    assert body == {
        "stage": "running",
        "activity": "a" * 500,
        "stdout_tail": "out",
        "last_output_at": 1792670029,
        "events": [
            {"id": "j:1", "at": 1.0, "kind": "tool", "text": "Read: A.cs"},
            {"id": "j:2", "at": 2.0, "kind": "status", "text": "x"},
        ],
        "usage": {"model": "m", "source": "codex-cli", "input_tokens": 10},
    }


def test_server_event_budget_keeps_the_newest() -> None:
    events = [
        {"id": f"j:{i}", "at": i, "kind": "message", "text": "x" * 2000}
        for i in range(100)
    ]
    kept = progress_body({"events": events})["events"]
    assert len(kept) == 30 and kept[-1]["id"] == "j:99" and kept[0]["id"] == "j:70"
