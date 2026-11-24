"""Opens a chosen Claude Code chat with Remote Control, so the user can continue it
from claude.ai/code or the Claude mobile app.

It runs the official `claude --resume <session> --remote-control <name>` in a new
console window on the PC, in the chat's project folder. Closing that window ends
Remote Control.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .coding import activity_report

BUSY_STATES = {"working", "approval", "question", "waiting"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RemoteControlError(Exception):
    pass


def claude_executable() -> str | None:
    found = sorted(
        (Path.home() / ".vscode" / "extensions").glob(
            "anthropic.claude-code-*/resources/native-binary/claude.exe"
        )
    )
    return str(found[-1]) if found else shutil.which("claude")


def find_chat(query: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    words = [w for w in re.split(r"\s+", query.casefold()) if w]
    if not words:
        raise RemoteControlError(
            "Tell me which Claude chat: a word from its title or project."
        )
    claude = [s for s in sessions if s.get("agent") == "claude" and s.get("session_id")]

    def text(session: dict[str, Any]) -> str:
        return " ".join(
            str(session.get(k) or "") for k in ("title", "project", "request")
        ).casefold()

    matches = [s for s in claude if all(w in text(s) for w in words)]
    if not matches:
        titles = (
            "; ".join(f"{s.get('title') or s['project']}" for s in claude) or "none"
        )
        raise RemoteControlError(
            f"No recent Claude chat matches {query!r}. Recent chats: {titles}"
        )
    exact = [
        s for s in matches if (s.get("title") or "").casefold() == query.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    if len(matches) > 1:
        titles = "; ".join(f"{s.get('title') or s['project']}" for s in matches)
        raise RemoteControlError(f"Several chats match {query!r}: {titles}. Which one?")
    return matches[0]


def start(query: str, *, launch: bool = True) -> dict[str, Any]:
    executable = claude_executable()
    if executable is None:
        raise RemoteControlError("Claude Code is not installed on this computer.")
    chat = find_chat(
        query, activity_report(agent="claude", hours=72, limit=10)["sessions"]
    )
    if chat["state"] in BUSY_STATES and chat["minutes_ago"] < 5:
        raise RemoteControlError(
            "That chat is busy in VS Code right now. Wait until it finishes, so two "
            "copies do not write to the same conversation."
        )
    session_id = str(chat["session_id"])
    if not UUID.match(session_id):
        raise RemoteControlError("That chat has an unexpected id.")
    folder = chat.get("project_folder") or chat.get("folder")
    if not folder or not Path(folder).is_dir():
        raise RemoteControlError("The chat's project folder is not available.")
    name = f"JARVIS: {chat.get('title') or chat['project']}"[:60]
    if launch:
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        subprocess.Popen(
            [executable, "--resume", session_id, "--remote-control", name],
            cwd=folder,
            creationflags=flags,
        )
    return {
        "chat": chat.get("title") or chat["project"],
        "project": chat["project"],
        "remote_control_name": name,
        "how_to_use": "Open the Claude app or claude.ai/code and pick this session. "
        "Closing the console window on the PC ends Remote Control.",
    }
