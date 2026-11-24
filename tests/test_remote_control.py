"""Choosing and opening a Claude Code chat with Remote Control."""

import pytest

from src.jarvis_desktop import remote_control
from src.jarvis_desktop.remote_control import RemoteControlError, find_chat, start

SID = "50b1cc8d-fbca-4ec4-a23b-2736679286ef"


def chat(
    title: str, project: str, state: str = "finished", minutes: int = 30, sid: str = SID
) -> dict:
    return {
        "agent": "claude",
        "title": title,
        "project": project,
        "request": "",
        "state": state,
        "minutes_ago": minutes,
        "session_id": sid,
        "project_folder": ".",
    }


SESSIONS = [
    chat("Migração do JARVIS ao servidor", "J.A.R.V.I.S"),
    chat(
        "Auditoria de salas e campanhas",
        "Scripts",
        sid="11111111-2222-3333-4444-555555555555",
    ),
    chat("Auditoria de guildas", "Scripts", sid="66666666-2222-3333-4444-555555555555"),
    {"agent": "codex", "title": None, "project": "J.A.R.V.I.S", "session_id": "x"},
]


def test_find_chat_by_words_and_ambiguity() -> None:
    assert find_chat("jarvis servidor", SESSIONS)["project"] == "J.A.R.V.I.S"
    assert find_chat("salas", SESSIONS)["title"] == "Auditoria de salas e campanhas"
    with pytest.raises(RemoteControlError, match="Several chats match"):
        find_chat("auditoria", SESSIONS)
    with pytest.raises(RemoteControlError, match="Recent chats"):
        find_chat("receitas", SESSIONS)
    with pytest.raises(RemoteControlError, match="Tell me which"):
        find_chat("  ", SESSIONS)


def test_start_refuses_busy_chat_and_builds_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_control, "claude_executable", lambda: "claude.exe")
    busy = [
        chat(
            "Migração do JARVIS ao servidor", "J.A.R.V.I.S", state="working", minutes=1
        )
    ]
    monkeypatch.setattr(
        remote_control, "activity_report", lambda **_: {"sessions": busy}
    )
    with pytest.raises(RemoteControlError, match="busy"):
        start("jarvis", launch=False)

    launched = []
    monkeypatch.setattr(
        remote_control, "activity_report", lambda **_: {"sessions": SESSIONS}
    )
    monkeypatch.setattr(
        remote_control.subprocess,
        "Popen",
        lambda args, **kw: launched.append((args, kw)),
    )
    result = start("jarvis")
    assert result["remote_control_name"] == "JARVIS: Migração do JARVIS ao servidor"
    args, kwargs = launched[0]
    assert args == [
        "claude.exe",
        "--resume",
        SID,
        "--remote-control",
        "JARVIS: Migração do JARVIS ao servidor",
    ]
    assert kwargs["cwd"] == "."
