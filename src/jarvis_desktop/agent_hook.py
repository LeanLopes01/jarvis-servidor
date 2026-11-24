"""Hook command for Claude Code, Codex and Antigravity: records each session's latest event.

Claude and Codex run it asynchronously; Antigravity runs it synchronously and expects JSON:

    python agent_hook.py claude                      # from ~/.claude/settings.json
    python agent_hook.py codex                       # from ~/.codex/hooks.json
    python agent_hook.py antigravity PreInvocation   # from ~/.gemini/config/hooks.json
    python agent_hook.py antigravity Stop            # from ~/.gemini/config/hooks.json

It reads the hook's JSON from stdin and replaces
%LOCALAPPDATA%/JarvisDesktop/agent-events/<agent>-<session>.json with the event, so
the coding report knows exactly when an agent is waiting for approval or for an
answer.

Every Stop and every approval or question request is also written to its own file in
%LOCALAPPDATA%/JarvisDesktop/outbox/. That file stays until the server confirms it
stored the event, so two quick finishes or a restart lose nothing; the helper sends
it (see outbox.py). Standard library only, and it always exits 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

KEEP_DAYS = 7
QUESTION_TOOLS = {"AskUserQuestion"}


def events_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "JarvisDesktop" / "agent-events"


def outbox_dir() -> Path:
    return events_dir().parent / "outbox"


def host_hint(
    agent: str,
    env: dict[str, str] | os._Environ[str] = os.environ,
    transcript_path: str | None = None,
) -> str:
    """Which app ran the agent, as far as the hook's environment tells."""
    if agent == "claude" and env.get("CLAUDE_CODE_ENTRYPOINT"):
        return env["CLAUDE_CODE_ENTRYPOINT"][:40]
    if agent == "antigravity" and transcript_path:
        norm = transcript_path.replace("\\", "/").casefold()
        if "/antigravity-cli/" in norm:
            return "cli"
        if "/antigravity-ide/" in norm:
            return "vscode"
        if "/antigravity/" in norm:
            return "Antigravity"
    if env.get("TERM_PROGRAM") == "vscode" or env.get("VSCODE_PID"):
        return "vscode"
    return "unknown"


def summarize(agent: str, data: dict, event_hint: str = "") -> dict:
    tool_call = data.get("toolCall") if isinstance(data.get("toolCall"), dict) else {}
    raw_input = data.get("tool_input")
    if not isinstance(raw_input, dict):
        raw_input = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    tool_input = raw_input
    detail = (
        tool_input.get("description")
        or tool_input.get("command")
        or tool_input.get("CommandLine")
        or tool_input.get("file_path")
        or tool_input.get("Path")
        or tool_input.get("url")
        or ""
    )
    workspaces = (
        data.get("workspacePaths")
        if isinstance(data.get("workspacePaths"), list)
        else []
    )
    cwd = data.get("cwd") or (workspaces[0] if workspaces else None)
    return {
        "agent": agent,
        "session_id": str(data.get("session_id") or data.get("conversationId") or ""),
        "event": str(
            data.get("hook_event_name") or data.get("hookEventName") or event_hint or ""
        ),
        "notification_type": data.get("notification_type"),
        "termination_reason": data.get("terminationReason"),
        "tool": data.get("tool_name") or tool_call.get("name"),
        "detail": str(detail)[:200] if detail else None,
        "message": str(data.get("message") or data.get("last_assistant_message") or "")[
            :300
        ]
        or None,
        "transcript_path": data.get("transcript_path") or data.get("transcriptPath"),
        "cwd": cwd,
        "ts": time.time(),
    }


def record(
    agent: str, data: dict, directory: Path, event_hint: str = ""
) -> Path | None:
    event = summarize(agent, data, event_hint)
    session = re.sub(r"[^A-Za-z0-9_-]", "_", event["session_id"])[:100]
    if not session:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{agent}-{session}.json"
    previous: dict | None = None
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    # Remember when the current turn began, across the events that follow it, so a
    # Stop can tell a long task from a quick reply.
    if event["event"] == "UserPromptSubmit" or (
        event["event"] in {"SessionStart", "PreInvocation"}
        and (not previous or previous.get("event") == "Stop")
    ):
        event["turn_started_at"] = event["ts"]
    else:
        event["turn_started_at"] = (previous or {}).get("turn_started_at")
    _write_atomic(directory, target, event)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in directory.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return target


def _write_atomic(directory: Path, target: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, delete=False, suffix=".tmp", encoding="utf-8"
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False)
    os.replace(tmp.name, target)


def outbox_entry(agent: str, data: dict, event: dict) -> dict | None:
    """The durable event for a finished turn or a request for the user, or None."""
    name = event["event"]
    if name == "Stop":
        kind = "agent_finished"
        status = (
            "interrupted"
            if data.get("terminationReason") == "USER_CANCELED"
            else "turn_completed"
        )
    elif name == "PermissionRequest":
        kind = "agent_waiting"
        status = "question" if event.get("tool") in QUESTION_TOOLS else "approval"
    else:
        return None
    turn_id = (
        str(
            data.get("turn_id")
            or data.get("executionNum")
            or data.get("invocationNum")
            or ""
        )[:120]
        or None
    )
    if turn_id:
        # A retried hook for the same turn maps to the same event.
        seed = f"{agent}|{event['session_id']}|{turn_id}|{kind}|{event.get('tool')}"
        event_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
    else:
        event_id = uuid.uuid4().hex
    return {
        "event_id": event_id,
        "kind": kind,
        "status": status,
        "agent": agent,
        "host": host_hint(agent, transcript_path=event.get("transcript_path")),
        "session_id": event["session_id"],
        "turn_id": turn_id,
        "cwd": event.get("cwd"),
        "transcript_path": event.get("transcript_path"),
        "tool": event.get("tool"),
        "detail": event.get("detail"),
        # The complete final answer; the helper trims and redacts it before sending.
        "message": str(data.get("last_assistant_message") or data.get("message") or "")
        or None,
        "continued": bool(data.get("stop_hook_active")),
        "started_at": event.get("turn_started_at"),
        "finished_at": event["ts"],
        "recorded_at": time.time(),
    }


def queue_event(agent: str, data: dict, event: dict, directory: Path) -> Path | None:
    entry = outbox_entry(agent, data, event)
    if entry is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    # Millisecond prefix: the helper sends the oldest first.
    target = (
        directory / f"{int(entry['recorded_at'] * 1000):013d}-{entry['event_id']}.json"
    )
    _write_atomic(directory, target, entry)
    return target


def main() -> None:
    agent = (
        sys.argv[1]
        if len(sys.argv) > 1 and sys.argv[1] in {"claude", "codex", "antigravity"}
        else "claude"
    )
    event_hint = sys.argv[2] if len(sys.argv) > 2 else ""
    resolved_event = event_hint
    try:
        data = json.loads(
            sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}"
        )
        if isinstance(data, dict):
            resolved_event = str(
                data.get("hook_event_name")
                or data.get("hookEventName")
                or event_hint
                or ""
            )
            target = record(agent, data, events_dir(), event_hint)
            if target is not None:
                event = json.loads(target.read_text(encoding="utf-8"))
                queue_event(agent, data, event, outbox_dir())
    except Exception:  # a hook must never disturb the agent
        pass
    finally:
        if agent == "antigravity":
            reply = {"decision": "stop"} if resolved_event == "Stop" else {}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
