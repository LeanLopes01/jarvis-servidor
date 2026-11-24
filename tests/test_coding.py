"""Coding-agent activity report from Claude Code and Codex session logs."""

import json
import os
from pathlib import Path

import pytest
from livekit.agents.llm import ToolError

from src.coding_agents import CodingAgentTools, CodingReportError
from src.jarvis_desktop.actions import ActionError, coding_activity
from src.jarvis_desktop.coding import (
    activity_report,
    parse_antigravity,
    parse_claude,
    parse_codex,
    redact,
)

NOW = 1_800_000_000.0


def iso(offset: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(NOW + offset, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def write(path: Path, records: list[dict], age: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    os.utime(path, (NOW + age, NOW + age))
    return path


def aged(records: list[dict], seconds: float) -> list[dict]:
    """The same session, as if it had happened `seconds` earlier."""
    from datetime import datetime, timedelta

    shifted = []
    for record in records:
        record = dict(record)
        if "timestamp" in record:
            when = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            record["timestamp"] = (
                (when - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
            )
        shifted.append(record)
    return shifted


def claude_records(*, last: str, offset: float = -5) -> list[dict]:
    base = {
        "cwd": "c:\\Projetos\\DiceVTT",
        "entrypoint": "claude-vscode",
        "isSidechain": False,
    }
    records = [
        {"type": "ai-title", "aiTitle": "Corrigir salas de campanha"},
        {
            **base,
            "type": "user",
            "timestamp": iso(-120),
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<ide_selection>codigo</ide_selection>"},
                    {
                        "type": "text",
                        "text": "Corrija o bug das salas. A senha do banco é: hunter2abc",
                    },
                ],
            },
        },
        {
            **base,
            "type": "assistant",
            "timestamp": iso(-100),
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "C:\\Projetos\\DiceVTT\\Salas.cs"},
                    },
                ],
            },
        },
        {
            **base,
            "type": "user",
            "timestamp": iso(-99),
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                ],
            },
        },
        {
            **base,
            "type": "assistant",
            "isSidechain": True,
            "timestamp": iso(-98),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "subagent chatter that must be ignored"},
                ],
            },
        },
    ]
    if last == "finished":
        records.append(
            {
                **base,
                "type": "assistant",
                "timestamp": iso(offset),
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "text",
                            "text": "Corrigi Salas.cs e os testes passaram. Token: sk-abcdefghijklmnopqrstuvwxyz123456",
                        },
                    ],
                },
            }
        )
    elif last == "pending":
        records.append(
            {
                **base,
                "type": "assistant",
                "timestamp": iso(offset),
                "message": {
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Bash",
                            "input": {
                                "command": "npm test",
                                "description": "Run the test suite",
                            },
                        },
                    ],
                },
            }
        )
    elif last == "interrupted":
        records.append(
            {
                **base,
                "type": "user",
                "timestamp": iso(offset),
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[Request interrupted by user]"},
                    ],
                },
            }
        )
    records.append(
        {
            "type": "last-prompt",
            "lastPrompt": "Corrija o bug das salas. PW='servidor_pessoal'",
        }
    )
    return records


def codex_records(*, finished: bool, offset: float = -10) -> list[dict]:
    records = [
        {
            "timestamp": iso(-300),
            "type": "session_meta",
            "payload": {"cwd": "c:\\J.A.R.V.I.S", "originator": "codex_vscode"},
        },
        {
            "timestamp": iso(-200),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": iso(-199),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "# Context from my IDE setup:\n\n## Active file: x\n\n## My request for Codex:\nAtualize o plano do servidor",
                    },
                ],
            },
        },
        {
            "timestamp": iso(-150),
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "exec",
                "input": 'text(await tools.exec_command({cmd:"Get-Content PLANO.md -Raw"}))',
            },
        },
        {
            "timestamp": iso(-149),
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": "...",
            },
        },
        {
            "timestamp": iso(-100),
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "c2",
                "name": "exec",
                "input": "text(await tools.apply_patch(`*** Begin Patch\n*** Update File: C:/J.A.R.V.I.S/PLANO.md\n@@`))",
            },
        },
    ]
    if finished:
        records += [
            {
                "timestamp": iso(-99),
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "c2",
                    "output": "ok",
                },
            },
            {
                "timestamp": iso(offset),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Plano atualizado com a seção 13.",
                },
            },
        ]
    return records


def test_redact_removes_passwords_and_tokens() -> None:
    text = redact(
        "Sobre a senha ela é: servidor_pessoal; PW='servidor_pessoal' "
        "password=hunter2 key sk-abcdefghijklmnopqrstuvwxyz1234 jd1.AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    )
    assert "servidor_pessoal" not in text
    assert "hunter2" not in text
    assert "sk-abc" not in text and "jd1.AbC" not in text
    assert text.count("[removido]") >= 4


def test_claude_finished_session(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", claude_records(last="finished"))
    report = parse_claude(path, NOW)
    assert report is not None
    assert (report.agent, report.project, report.source) == (
        "claude",
        "DiceVTT",
        "VS Code",
    )
    assert report.title == "Corrigir salas de campanha"
    assert report.state == "finished"
    assert report.files_changed == ["Salas.cs"]
    assert report.recent_actions == ["Edit Salas.cs"]
    assert "testes passaram" in (report.last_message or "")
    for text in (report.request or "", report.last_message or ""):
        assert "servidor_pessoal" not in text and "sk-abc" not in text


def test_claude_pending_tool_becomes_waiting(tmp_path: Path) -> None:
    fresh = parse_claude(
        write(tmp_path / "a.jsonl", claude_records(last="pending", offset=-5)), NOW
    )
    assert fresh is not None and fresh.state == "working" and "Bash" in fresh.detail
    old = parse_claude(
        write(tmp_path / "b.jsonl", claude_records(last="pending", offset=-90)), NOW
    )
    assert old is not None and old.state == "waiting" and "approval" in old.detail
    assert old.recent_actions[-1] == "Bash: Run the test suite"


def test_claude_interrupted_and_stalled(tmp_path: Path) -> None:
    interrupted = parse_claude(
        write(tmp_path / "i.jsonl", claude_records(last="interrupted")), NOW
    )
    assert interrupted is not None and interrupted.state == "interrupted"
    stalled_records = claude_records(last="none")
    # An hour later, a session still mid-turn with no new records is stalled.
    stalled = parse_claude(write(tmp_path / "s.jsonl", stalled_records), NOW + 3600)
    assert stalled is not None and stalled.state == "stalled"


def test_codex_sessions(tmp_path: Path) -> None:
    done = parse_codex(
        write(tmp_path / "done.jsonl", codex_records(finished=True)), NOW
    )
    assert done is not None
    assert (done.agent, done.project, done.source, done.state) == (
        "codex",
        "J.A.R.V.I.S",
        "VS Code",
        "finished",
    )
    assert done.request == "Atualize o plano do servidor"
    assert done.last_message == "Plano atualizado com a seção 13."
    assert done.files_changed == ["PLANO.md"]
    assert done.recent_actions[0].startswith("command: Get-Content PLANO.md")

    running = parse_codex(
        write(tmp_path / "run.jsonl", codex_records(finished=False)), NOW
    )
    assert (
        running is not None and running.state == "waiting"
    )  # patch call 100 s without output


def test_activity_report_filters_by_agent_and_age(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    write(
        claude / "projects" / "c--Projetos-DiceVTT" / "new.jsonl",
        claude_records(last="finished"),
    )
    write(
        claude / "projects" / "c--Old" / "old.jsonl",
        aged(claude_records(last="finished"), 5 * 86400),
        age=-5 * 86400,
    )
    write(
        codex / "sessions" / "2026" / "10" / "16" / "rollout-x.jsonl",
        codex_records(finished=True),
        age=-60,
    )

    report = activity_report(now=NOW, claude_root=claude, codex_root=codex)
    assert [s["agent"] for s in report["sessions"]] == ["claude", "codex"]
    assert "servidor_pessoal" not in json.dumps(report)

    only_codex = activity_report(
        agent="codex", now=NOW, claude_root=claude, codex_root=codex
    )
    assert [s["agent"] for s in only_codex["sessions"]] == ["codex"]
    longer = activity_report(
        hours=72, limit=10, now=NOW - 2 * 86400, claude_root=claude, codex_root=codex
    )
    assert any(s["project"] == "DiceVTT" for s in longer["sessions"])


async def test_desktop_action_validates_parameters() -> None:
    with pytest.raises(ActionError):
        await coding_activity({"agent": "gpt"})
    with pytest.raises(ActionError):
        await coding_activity({"hours": "muitas"})


class FakeBackend:
    def __init__(self, result: dict | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, float]] = []

    async def report(self, agent: str, hours: float) -> dict:
        self.calls.append((agent, hours))
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


async def test_tool_clamps_hours_and_reports_errors() -> None:
    backend = FakeBackend({"sessions": []})
    tools = CodingAgentTools(backend)
    result = await tools.check_coding_agents(None, "claude", 500)  # type: ignore[arg-type]
    assert backend.calls == [("claude", 72.0)]
    assert result["note"] == "No coding agent sessions in that period."

    failing = CodingAgentTools(
        FakeBackend(
            CodingReportError("The user's computer is not connected right now.")
        )
    )
    with pytest.raises(ToolError, match="not connected"):
        await failing.check_coding_agents(None)  # type: ignore[arg-type]
    assert CodingAgentTools(None).tools == []


def test_open_codex_file_with_stale_mtime_is_still_recent(tmp_path: Path) -> None:
    """Windows keeps the old modification time while Codex holds the file open."""
    codex = tmp_path / "codex"
    write(
        codex / "sessions" / "2026" / "10" / "15" / "rollout-open.jsonl",
        codex_records(finished=True, offset=-60),
        age=-20 * 3600,
    )
    report = activity_report(
        agent="codex", hours=1, now=NOW, claude_root=tmp_path, codex_root=codex
    )
    assert [s["minutes_ago"] for s in report["sessions"]] == [1]


def test_claude_project_is_where_the_session_started(tmp_path: Path) -> None:
    records = claude_records(last="finished")
    records[-2]["cwd"] = r"c:\Projetos\DiceVTT\Assets\Scripts"  # the shell moved later
    report = parse_claude(write(tmp_path / "s.jsonl", records), NOW)
    assert report is not None
    assert report.project == "DiceVTT"
    assert report.folder == r"c:\Projetos\DiceVTT\Assets\Scripts"


def test_antigravity_transcript_parsing_ignores_thinking(tmp_path: Path) -> None:
    transcript = (
        tmp_path
        / "antigravity-cli"
        / "brain"
        / "conv-agy-1"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    records = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": iso(-60),
            "content": "<USER_REQUEST>\nVincular Antigravity no Orquestrador\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": iso(-45),
            "thinking": "SEGREDO_DE_PENSAMENTO_INTERNO_NAO_VAZAR",
            "tool_calls": [
                {
                    "name": "edit_file",
                    "args": {
                        "Path": '"D:\\\\Projetos\\\\Orquestrador\\\\src\\\\ProjectsPanel.tsx"'
                    },
                }
            ],
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "created_at": iso(-40),
            "content": "Edited file",
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "created_at": iso(-10),
            "thinking": "OUTRO_PENSAMENTO_INTERNO",
            "content": "Integração concluída no ProjectsPanel.",
        },
    ]
    write(transcript, records)
    report = parse_antigravity(transcript, NOW)
    assert report is not None
    assert (report.agent, report.source, report.state, report.session_id) == (
        "antigravity",
        "CLI",
        "finished",
        "conv-agy-1",
    )
    assert report.project == "src"
    assert report.request == "Vincular Antigravity no Orquestrador"
    assert report.last_message == "Integração concluída no ProjectsPanel."
    assert report.files_changed == ["ProjectsPanel.tsx"]
    assert "SEGREDO_DE_PENSAMENTO" not in json.dumps(report.__dict__)

    only_agy = activity_report(
        agent="antigravity",
        now=NOW,
        claude_root=tmp_path / "empty-c",
        codex_root=tmp_path / "empty-x",
        antigravity_root=tmp_path,
    )
    assert [s["agent"] for s in only_agy["sessions"]] == ["antigravity"]


def test_latest_reports_reads_all_three_and_prefers_full_antigravity_log(
    tmp_path: Path,
) -> None:
    from src.jarvis_desktop.coding import latest_reports

    claude_log = tmp_path / "claude" / "projects" / "d--App" / "sess-1.jsonl"
    write(
        claude_log,
        [
            {"type": "user", "cwd": "D:/App", "timestamp": iso(-20), "message": {"content": "Fix bug"}},
            {
                "type": "assistant",
                "cwd": "D:/App",
                "timestamp": iso(-15),
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Relatório completo do Claude com senha: secret123."}]},
            },
        ],
    )
    codex_log = tmp_path / "codex" / "sessions" / "2026" / "11" / "24" / "rollout-2026-11-24T12-00-00-aaaa-bbbb-cccc-dddd-eeee.jsonl"
    write(
        codex_log,
        [
            {"timestamp": iso(-12), "payload": {"type": "session_meta", "cwd": "D:/App", "originator": "codex_vscode"}},
            {"timestamp": iso(-10), "payload": {"type": "task_complete", "last_agent_message": "Relatório completo do Codex."}},
        ],
    )
    agy_dir = tmp_path / "agy" / "antigravity" / "brain" / "conv-full" / ".system_generated" / "logs"
    write(
        agy_dir / "transcript.jsonl",
        [
            {"type": "USER_INPUT", "created_at": iso(-8), "content": "<USER_REQUEST>Status</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "created_at": iso(-5), "content": "Cortado...", "truncated_fields": ["content"]},
        ],
    )
    write(
        agy_dir / "transcript_full.jsonl",
        [
            {"type": "USER_INPUT", "created_at": iso(-8), "content": "<USER_REQUEST>Status</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "created_at": iso(-5), "content": "Relatório completo do Antigravity sem corte."},
        ],
    )

    out = latest_reports(
        agent="all",
        now=NOW,
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        antigravity_root=tmp_path / "agy",
    )
    reports = out["reports"]
    assert "Relatório completo do Claude" in reports["claude"]["report"]
    assert "secret123" not in reports["claude"]["report"]
    assert reports["codex"]["report"] == "Relatório completo do Codex."
    assert reports["antigravity"]["report"] == "Relatório completo do Antigravity sem corte."


@pytest.mark.asyncio
async def test_get_latest_agent_report_tool() -> None:
    calls: list[tuple[str, float]] = []

    class FakeBackend:
        async def report(self, agent: str, hours: float) -> dict:
            return {"sessions": []}

        async def latest_report(self, agent: str, hours: float) -> dict:
            calls.append((agent, hours))
            return {"reports": {"antigravity": {"report": "Tudo pronto."}}}

        async def usage(self) -> dict:
            return {}

        async def remote_control(self, chat: str) -> dict:
            return {}

    tools = CodingAgentTools(FakeBackend())
    result = await tools.get_latest_agent_report(None, "antigravity", 24)  # type: ignore[arg-type]
    assert calls == [("antigravity", 24.0)]
    assert result["reports"]["antigravity"]["report"] == "Tudo pronto."
