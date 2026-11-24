"""Read-only report of what the coding agents on this PC are doing.

Claude Code and Codex (both the VS Code extensions and the CLIs) keep every session
as a JSONL file in the user's profile:

    ~/.claude/projects/<project>/<session>.jsonl          (CLAUDE_CONFIG_DIR overrides)
    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl          (CODEX_HOME overrides)

Only the end of each recent file is read. The report keeps short excerpts with
secrets removed, because it goes to the server and then to the voice model.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from .agent_hook import events_dir

TAIL_BYTES = 768 * 1024
HEAD_BYTES = 64 * 1024
LAST_RECORD_BYTES = 8 * 1024
TEXT_LIMIT = 280
LAST_MESSAGE_LIMIT = 1200
REPORT_TEXT_LIMIT = 4000
ACTION_LIMIT = 6
FILE_LIMIT = 8
ACTIVE_MINUTES = 20  # a "working" session with no new records after this is stalled
APPROVAL_SECONDS = 20  # a tool call this old without a result may await approval
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_SECRET_LABEL = re.compile(
    r"(?i)\b(senha|password|passwd|pwd|pw|secret|token|api[_-]?key)\b"
    r"((?:[^\n]{0,15}?[:=])|(?:\s+(?:\w+\s+)?(?:é|e|is)\s*:?))\s*['\"]?([^\s'\",;]{3,})"
)
_SECRET_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|jd1\.[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|[A-Za-z0-9+/_-]{40,})"
)


def redact(text: str) -> str:
    text = _SECRET_LABEL.sub(lambda m: f"{m.group(1)}{m.group(2)} [removido]", text)
    return _SECRET_SHAPES.sub("[removido]", text)


def clip(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if not text:
        return None
    text = redact(re.sub(r"\s+", " ", text).strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class SessionReport:
    agent: str
    project: str
    folder: str | None
    source: str
    title: str | None
    state: str
    detail: str
    minutes_ago: int
    request: str | None = None
    last_message: str | None = None
    recent_actions: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    # "exact" when the agent's hooks report approvals and questions for this session,
    # "estimate" when a long-running tool call is the only clue.
    detection: str = "estimate"
    session_id: str | None = None
    project_folder: str | None = None


WAITING_NOTIFICATIONS = {
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
QUESTION_TOOLS = {"AskUserQuestion", "ask_question"}
ANTIGRAVITY_EDIT_TOOLS = {
    "edit_file",
    "write_file",
    "replace_file_content",
    "multi_replace_file_content",
}


SHELL_TOOLS = {"Bash", "PowerShell"}


def _signature(command: str) -> str | None:
    """A literal piece of the command that survives the agents' shell quoting."""
    runs = re.findall(r"[A-Za-z0-9 ._/:=,-]{10,}", command)
    best = max(runs, key=len, default="").strip()
    return best[:50] if len(best) >= 10 else None


def command_running(command: str, since: float) -> bool:
    """Whether a process started for this call is running, i.e. it was approved."""
    signature = _signature(command)
    if signature is None:
        return False
    try:
        import psutil
    except ImportError:
        return False
    for process in psutil.process_iter(["cmdline", "create_time"]):
        try:
            started = process.info["create_time"] or 0
            line = " ".join(process.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if started >= since - 10 and signature in line:
            return True
    return False


def _codex_command(payload: dict[str, Any]) -> str | None:
    raw = payload.get("input") or payload.get("arguments") or ""
    raw = raw if isinstance(raw, str) else json.dumps(raw)
    match = re.search(r"cmd\s*[:=]\s*\"((?:[^\"\\]|\\.){1,400})", raw)
    return match.group(1) if match else None


def _attention(
    pending: dict[str, tuple[str, float, str | None]],
    event: dict[str, Any] | None,
    last_time: float,
) -> tuple[str, str] | None:
    """Exact waiting states from the agent's own hook event, when there is one."""
    if pending and list(pending.values())[-1][0] in QUESTION_TOOLS:
        return "question", "is asking you a question and waiting for your answer"
    if event is None:
        return None
    asked = (
        event.get("event") == "PermissionRequest"
        or event.get("notification_type") == "permission_prompt"
    )
    if asked and pending:
        tool, since, command = list(pending.values())[-1]
        # The permission was requested for the call that is still open, and the command
        # has not started: once approved, its process shows up and it is just running.
        if event.get("ts", 0) >= since - 2 and not (
            command and command_running(command, since)
        ):
            what = clip(event.get("detail"), 80)
            name = event.get("tool") or tool
            return "approval", f"waiting for your approval to run {name}" + (
                f": {what}" if what else ""
            )
    if (
        event.get("notification_type") in WAITING_NOTIFICATIONS
        and event.get("ts", 0) >= last_time - 2
    ):
        return "question", "is asking you a question and waiting for your answer"
    return None


def _open_call_state(
    pending: dict[str, tuple[str, float, str | None]], now: float, hooked: bool
) -> tuple[str, str]:
    tool, since, _ = list(pending.values())[-1]
    seconds = int(now - since)
    if hooked:
        # The hooks would have reported a permission prompt: this is just slow.
        return "working", f"running {tool}" + (
            f" for {seconds} s" if seconds > 20 else ""
        )
    if seconds > APPROVAL_SECONDS:
        return "waiting", (
            f"{tool} has been running for {seconds} s; it may be waiting for your "
            "approval (estimate: hooks are not installed for this session)"
        )
    return "working", f"running {tool}"


def _read_records(path: Path, *, head: bool = False) -> list[dict[str, Any]]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if head:
            data = handle.read(HEAD_BYTES)
        else:
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if head:
        lines = lines[:-1] if len(data) == HEAD_BYTES else lines
    elif size > TAIL_BYTES:
        lines = lines[1:]  # the first line was cut by the seek
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _timestamp(record: dict[str, Any]) -> float | None:
    value = record.get("timestamp") or record.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _name(path: str | None) -> str | None:
    if not path:
        return None
    name = PurePosixPath(path.replace("\\", "/")).name
    return None if not name or re.fullmatch(r"[A-Za-z]:", name) else name


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"text", "input_text", "output_text"}
    ]


def _human_text(content: Any) -> str | None:
    """The user's own words, without IDE context and reminders the tools inject."""
    parts = [
        text
        for text in _texts(content)
        if text.strip() and not text.lstrip().startswith("<")
    ]
    return "\n".join(parts) or None


def _describe_claude_tool(name: str, data: dict[str, Any]) -> str:
    if name in EDIT_TOOLS or name == "Read":
        return f"{name} {_name(data.get('file_path') or data.get('notebook_path')) or ''}".strip()
    if name in {"Bash", "PowerShell"}:
        return f"{name}: {clip(data.get('description') or data.get('command'), 70)}"
    if name in {"Grep", "Glob"}:
        return f"{name} {clip(data.get('pattern'), 40)}"
    if name in {"Agent", "Task"}:
        return f"subagent: {clip(data.get('description'), 60)}"
    if name in {"WebSearch", "WebFetch"}:
        return f"{name} {clip(data.get('query') or data.get('url'), 60)}"
    return name


def _launch_folder(path: Path, folders: set[str]) -> str | None:
    """The folder the session was opened in, which names the project.

    Claude Code stores a session under its launch path with every other character
    replaced by "-" (c:/J.A.R.V.I.S -> c--J-A-R-V-I-S); the shell may move later.
    """
    slug = path.parent.name.casefold()
    for folder in folders:
        if re.sub(r"[^A-Za-z0-9]", "-", folder).casefold() == slug:
            return folder
    return min(folders, key=len) if folders else None


def parse_claude(
    path: Path, now: float, event: dict[str, Any] | None = None
) -> SessionReport | None:
    records = _read_records(path)
    title = request = folder = source = None
    folders: set[str] = set()
    last_text = None
    actions: list[str] = []
    files: list[str] = []
    pending: dict[str, tuple[str, float, str | None]] = {}
    last: dict[str, Any] | None = None
    last_time = path.stat().st_mtime

    for record in records:
        kind = record.get("type")
        if kind == "ai-title":
            title = record.get("aiTitle") or title
        elif kind == "last-prompt":
            request = record.get("lastPrompt") or request
        if record.get("cwd"):
            folder = record["cwd"]
            folders.add(folder)
        if record.get("entrypoint"):
            source = record["entrypoint"]
        if kind not in {"user", "assistant"} or record.get("isSidechain"):
            continue
        message = record.get("message") or {}
        content = message.get("content")
        when = _timestamp(record) or last_time
        if kind == "assistant" and isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text", "").strip():
                    last_text = item["text"]
                elif item.get("type") == "tool_use":
                    data = (
                        item.get("input") if isinstance(item.get("input"), dict) else {}
                    )
                    actions.append(
                        _describe_claude_tool(item.get("name", "tool"), data)
                    )
                    command = (
                        data.get("command") if item.get("name") in SHELL_TOOLS else None
                    )
                    pending[item.get("id", "")] = (
                        item.get("name", "tool"),
                        when,
                        command,
                    )
                    if item.get("name") in EDIT_TOOLS:
                        changed = _name(
                            data.get("file_path") or data.get("notebook_path")
                        )
                        if changed:
                            files.append(changed)
        elif kind == "user":
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        pending.pop(item.get("tool_use_id", ""), None)
            human = _human_text(content)
            if human and not any(
                isinstance(i, dict) and i.get("type") == "tool_result"
                for i in content or []
            ):
                request = request if human.startswith("[Request interrupted") else human
        last = record
        last_time = when

    if last is None:
        return None
    age = now - last_time
    message = last.get("message") or {}
    content = message.get("content")
    attention = _attention(pending, event, last_time)
    if attention:
        state, detail = attention
    elif last.get("type") == "assistant":
        if pending:
            state, detail = _open_call_state(pending, now, event is not None)
        elif message.get("stop_reason") == "end_turn":
            state, detail = (
                "finished",
                "finished its turn and is waiting for your next message",
            )
        else:
            state, detail = "working", "thinking"
    elif any("[Request interrupted" in text for text in _texts(content)):
        state, detail = "interrupted", "you interrupted the last request"
    else:
        state, detail = "working", "working on the request"
    if state == "working" and age > ACTIVE_MINUTES * 60:
        state, detail = (
            "stalled",
            "no activity recently; it may have been closed or stopped",
        )

    return SessionReport(
        agent="claude",
        project=_name(_launch_folder(path, folders) or folder) or path.parent.name,
        session_id=path.stem,
        project_folder=_launch_folder(path, folders) or folder,
        folder=folder,
        source="VS Code" if source == "claude-vscode" else (source or "CLI"),
        title=clip(title, 100),
        state=state,
        detail=detail,
        minutes_ago=int(age // 60),
        request=clip(request),
        last_message=clip(last_text, LAST_MESSAGE_LIMIT),
        recent_actions=[clip(a, 90) or "" for a in actions[-ACTION_LIMIT:]],
        files_changed=list(dict.fromkeys(reversed(files)))[:FILE_LIMIT],
        detection="exact" if event is not None else "estimate",
    )


def _codex_request(text: str) -> str | None:
    marker = "## My request for Codex:"
    if marker in text:
        return text.split(marker, 1)[1].strip() or None
    if text.lstrip().startswith(("<", "# Context from my IDE setup", "# AGENTS.md")):
        return None
    return text.strip() or None


def _describe_codex_call(payload: dict[str, Any]) -> tuple[str, list[str]]:
    raw = payload.get("input") or payload.get("arguments") or ""
    raw = raw if isinstance(raw, str) else json.dumps(raw)
    files = [
        _name(m.strip()) or ""
        for m in re.findall(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"]+)", raw)
    ]
    if "apply_patch" in raw or files:
        return f"edited {', '.join(files[:3]) or 'files'}", files
    command = re.search(r"cmd\s*[:=]\s*\"((?:[^\"\\]|\\.){1,200})", raw)
    if command:
        return f"command: {clip(command.group(1), 70)}", []
    name = payload.get("name") or "tool"
    namespace = payload.get("namespace")
    return (f"{namespace}.{name}" if namespace else name), []


def parse_codex(
    path: Path, now: float, event: dict[str, Any] | None = None
) -> SessionReport | None:
    folder = source = request = last_text = None
    for record in _read_records(path, head=True)[:5]:
        payload = (
            record.get("payload") if isinstance(record.get("payload"), dict) else {}
        )
        if record.get("type") == "session_meta":
            folder = payload.get("cwd")
            source = payload.get("originator") or payload.get("source")
    started_in = folder
    records = _read_records(path)
    actions: list[str] = []
    files: list[str] = []
    pending: dict[str, tuple[str, float, str | None]] = {}
    state, detail = "finished", "idle"
    last_time = path.stat().st_mtime

    for record in records:
        payload = (
            record.get("payload") if isinstance(record.get("payload"), dict) else {}
        )
        kind = payload.get("type")
        when = _timestamp(record) or last_time
        last_time = when
        if record.get("type") == "turn_context" and payload.get("cwd"):
            folder = payload["cwd"]
        if kind == "message" and payload.get("role") == "user":
            request = (
                _codex_request("\n".join(_texts(payload.get("content")))) or request
            )
        elif kind == "message" and payload.get("role") == "assistant":
            last_text = "\n".join(_texts(payload.get("content"))) or last_text
        elif kind == "agent_message" and payload.get("message"):
            last_text = payload["message"]
        elif kind in {"custom_tool_call", "function_call", "local_shell_call"}:
            description, changed = _describe_codex_call(payload)
            actions.append(description)
            files.extend(changed)
            pending[payload.get("call_id", "")] = (
                description,
                when,
                _codex_command(payload),
            )
        elif kind in {
            "custom_tool_call_output",
            "function_call_output",
            "local_shell_call_output",
        }:
            pending.pop(payload.get("call_id", ""), None)
        elif kind == "patch_apply_end" and isinstance(payload.get("changes"), dict):
            files.extend(_name(p) or "" for p in payload["changes"])
        elif kind == "task_started":
            state, detail = "working", "working on the request"
        elif kind == "task_complete":
            state, detail = (
                "finished",
                "finished the task and is waiting for your next message",
            )
            last_text = payload.get("last_agent_message") or last_text
            pending.clear()
        elif kind == "turn_aborted":
            state, detail = "interrupted", "the last request was interrupted"
            pending.clear()

    if not records:
        return None
    age = now - last_time
    if state == "working" and pending:
        attention = _attention(pending, event, last_time)
        state, detail = attention or _open_call_state(pending, now, event is not None)
    if state == "working" and age > ACTIVE_MINUTES * 60:
        state, detail = (
            "stalled",
            "no activity recently; it may have been closed or stopped",
        )

    return SessionReport(
        agent="codex",
        project=_name(started_in or folder) or "unknown project",
        session_id="-".join(path.stem.split("-")[-5:]),
        project_folder=started_in or folder,
        folder=folder,
        source="VS Code" if source in {"codex_vscode", "vscode"} else (source or "CLI"),
        title=None,
        state=state,
        detail=detail,
        minutes_ago=int(age // 60),
        request=clip(request),
        last_message=clip(last_text, LAST_MESSAGE_LIMIT),
        recent_actions=[clip(a, 90) or "" for a in actions[-ACTION_LIMIT:]],
        files_changed=[f for f in dict.fromkeys(reversed(files)) if f][:FILE_LIMIT],
        detection="exact" if event is not None else "estimate",
    )


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def antigravity_dir() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_HOME") or Path.home() / ".gemini")


def antigravity_config_dir() -> Path:
    if os.environ.get("ANTIGRAVITY_CONFIG_DIR"):
        return Path(os.environ["ANTIGRAVITY_CONFIG_DIR"])
    if os.environ.get("CLAUDE_CONFIG_DIR") and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]).parent / "antigravity" / "config"
    return antigravity_dir() / "config"


def antigravity_roots(root: Path | None = None) -> list[Path]:
    base = root or antigravity_dir()
    roots = [
        base / sub / "brain"
        for sub in ("antigravity", "antigravity-cli", "antigravity-ide")
    ]
    if root is not None:
        roots.extend([root / "brain", root])
    return roots


def _agy_arg(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _antigravity_request(text: str) -> str | None:
    matches = re.findall(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.DOTALL)
    if matches:
        return matches[-1].strip() or None
    cleaned = re.sub(
        r"<(?:CONTEXT_SUMMARY|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</(?:CONTEXT_SUMMARY|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>",
        "",
        text,
        flags=re.DOTALL,
    ).strip()
    if not cleaned or cleaned.startswith("<"):
        return None
    return cleaned


def _describe_antigravity_call(
    call: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None]:
    name = str(call.get("name") or "tool")
    raw_args = call.get("args") if isinstance(call.get("args"), dict) else {}
    args = {k: _agy_arg(v) for k, v in raw_args.items()}
    file_arg = (
        args.get("Path")
        or args.get("TargetFile")
        or args.get("SearchPath")
        or args.get("DirectoryPath")
    )
    file_str = str(file_arg) if isinstance(file_arg, str) and file_arg else None
    changed = _name(file_str) if name in ANTIGRAVITY_EDIT_TOOLS and file_str else None
    command = (
        str(args["CommandLine"])
        if name == "run_command" and isinstance(args.get("CommandLine"), str)
        else None
    )
    if name in ANTIGRAVITY_EDIT_TOOLS or name in {"read_file", "view_file"}:
        desc = f"{name} {_name(file_str) or ''}".strip()
    elif name == "run_command":
        desc = f"run_command: {clip(str(args.get('toolSummary') or command or ''), 70)}"
    elif name in {"grep_search", "find_by_name"}:
        desc = f"{name} {clip(str(args.get('Query') or args.get('Pattern') or ''), 40)}".strip()
    elif name == "search_web":
        desc = f"search_web {clip(str(args.get('query') or ''), 60)}".strip()
    else:
        desc = name
    return desc, changed, command, file_str


def parse_antigravity(
    path: Path, now: float, event: dict[str, Any] | None = None
) -> SessionReport | None:
    records = _read_records(path)
    if not records:
        return None
    folder = str(event["cwd"]) if event and event.get("cwd") else None
    inferred_folder: str | None = None
    request = last_text = None
    actions: list[str] = []
    files: list[str] = []
    pending: dict[str, tuple[str, float, str | None]] = {}
    state, detail = "finished", "idle"
    last_time = path.stat().st_mtime

    for idx, record in enumerate(records):
        kind = record.get("type")
        when = _timestamp(record) or last_time
        last_time = when
        if kind == "USER_INPUT":
            content = record.get("content")
            if isinstance(content, str):
                request = _antigravity_request(content) or request
            pending.clear()
            state, detail = "working", "working on the request"
        elif kind == "PLANNER_RESPONSE":
            # Never inspect record.get("thinking"): only user-facing content and tool_calls.
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                last_text = content
            calls = record.get("tool_calls")
            if isinstance(calls, list) and calls:
                pending.clear()
                for c_idx, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    desc, changed, command, file_str = _describe_antigravity_call(call)
                    actions.append(desc)
                    if changed:
                        files.append(changed)
                    if (
                        inferred_folder is None
                        and file_str
                        and "/.gemini/" not in file_str.replace("\\", "/").casefold()
                    ):
                        norm_parent = PurePosixPath(file_str.replace("\\", "/")).parent.as_posix()
                        if norm_parent and norm_parent not in {".", "/"} and not re.fullmatch(r"[A-Za-z]:", norm_parent):
                            inferred_folder = (
                                file_str.rsplit("\\", 1)[0]
                                if "\\" in file_str and "/" not in file_str
                                else norm_parent
                            )
                    tool_name = str(call.get("name") or desc)
                    pending[f"{record.get('step_index', idx)}:{c_idx}"] = (
                        tool_name,
                        when,
                        command,
                    )
                state, detail = "working", f"running {list(pending.values())[-1][0]}"
            else:
                pending.clear()
                state, detail = (
                    "finished",
                    "finished its turn and is waiting for your next message",
                )
        else:
            if pending and not any(t in QUESTION_TOOLS for t, _, _ in pending.values()):
                pending.clear()
                state, detail = "working", "thinking"

    age = now - last_time
    if event and event.get("event") == "Stop" and event.get("ts", 0) >= last_time - 2:
        pending.clear()
        if event.get("termination_reason") == "USER_CANCELED":
            state, detail = "interrupted", "the last request was interrupted"
        else:
            state, detail = (
                "finished",
                "finished its turn and is waiting for your next message",
            )
    elif state == "working" and pending:
        attention = _attention(pending, event, last_time)
        state, detail = attention or _open_call_state(pending, now, event is not None)
    if state == "working" and age > ACTIVE_MINUTES * 60:
        state, detail = (
            "stalled",
            "no activity recently; it may have been closed or stopped",
        )

    resolved_folder = folder or inferred_folder
    parts = {p.casefold() for p in path.parts}
    if "antigravity-cli" in parts:
        source = "CLI"
    elif "antigravity-ide" in parts:
        source = "VS Code"
    else:
        source = "Antigravity"
    session_id = (
        path.parents[2].name
        if len(path.parents) >= 3 and path.parent.name == "logs"
        else path.stem
    )
    return SessionReport(
        agent="antigravity",
        project=_name(resolved_folder) or "Antigravity",
        session_id=session_id,
        project_folder=resolved_folder,
        folder=resolved_folder,
        source=source,
        title=None,
        state=state,
        detail=detail,
        minutes_ago=int(age // 60),
        request=clip(request),
        last_message=clip(last_text, LAST_MESSAGE_LIMIT),
        recent_actions=[clip(a, 90) or "" for a in actions[-ACTION_LIMIT:]],
        files_changed=[f for f in dict.fromkeys(reversed(files)) if f][:FILE_LIMIT],
        detection="exact" if event is not None else "estimate",
    )


def last_activity(path: Path) -> float:
    """Time of the last record in a session file.

    Codex keeps the active session's file open, and Windows does not refresh the
    modification time of an open file, so the timestamp inside the log is what counts.
    """
    mtime = path.stat().st_mtime
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - LAST_RECORD_BYTES))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        stamp = _timestamp(record) if isinstance(record, dict) else None
        if stamp is not None:
            return max(stamp, mtime)
    return mtime


def _recent(paths: list[Path], since: float) -> list[Path]:
    dated = []
    for path in paths:
        try:
            # Files untouched for a month are not scanned at all.
            if path.stat().st_mtime < since - 30 * 86400:
                continue
            when = last_activity(path)
        except OSError:
            continue
        if when >= since:
            dated.append((when, path))
    return [path for _, path in sorted(dated, reverse=True)]


def vscode_windows() -> list[str]:
    """Titles of open VS Code windows, e.g. 'agent.py - jarvis - Visual Studio Code'."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.endswith("Visual Studio Code"):
                    titles.append(clip(buffer.value, 120) or "")
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return titles


def load_events(directory: Path) -> list[dict[str, Any]]:
    """Latest hook event of each session, written by agent_hook.py."""
    events = []
    for path in directory.glob("*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(event, dict) and event.get("session_id"):
            events.append(event)
    return events


def _event_for(
    agent: str, path: Path, events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    target = str(path).replace("\\", "/").casefold()
    for event in events:
        if event.get("agent") != agent:
            continue
        transcript = (
            str(event.get("transcript_path") or "").replace("\\", "/").casefold()
        )
        sid = str(event["session_id"]).casefold()
        if transcript == target or (sid and sid in target):
            return event
    return None


def activity_report(
    *,
    agent: str = "all",
    hours: float = 12,
    limit: int = 5,
    now: float | None = None,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    antigravity_root: Path | None = None,
    events_root: Path | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    events = load_events(events_root or events_dir())
    since = now - min(max(hours, 0.25), 72) * 3600
    candidates: list[tuple[str, Path]] = []
    if agent in {"all", "claude"}:
        root = (claude_root or claude_dir()) / "projects"
        candidates += [("claude", p) for p in root.glob("*/*.jsonl")]
    if agent in {"all", "codex"}:
        root = (codex_root or codex_dir()) / "sessions"
        candidates += [("codex", p) for p in root.glob("*/*/*/*.jsonl")]
    if agent in {"all", "antigravity"} and (
        antigravity_root is not None or (claude_root is None and codex_root is None)
    ):
        seen_paths: set[Path] = set()
        for root in antigravity_roots(antigravity_root):
            for p in root.glob("*/.system_generated/logs/transcript.jsonl"):
                if p not in seen_paths:
                    seen_paths.add(p)
                    candidates.append(("antigravity", p))

    parsers = {
        "claude": parse_claude,
        "codex": parse_codex,
        "antigravity": parse_antigravity,
    }
    kinds = {path: kind for kind, path in candidates}
    sessions = []
    for path in _recent([path for _, path in candidates], since)[
        : min(max(limit, 1), 10)
    ]:
        parser = parsers[kinds[path]]
        try:
            report = parser(path, now, _event_for(kinds[path], path, events))
        except (OSError, ValueError):
            continue
        if report is not None:
            sessions.append(asdict(report))

    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "hours": hours,
        "sessions": sessions,
        "vscode_windows": vscode_windows(),
    }


def _full_last_message(agent: str, path: Path) -> str | None:
    """Latest full assistant output from a transcript (preferring full Antigravity log)."""
    target = path
    if agent == "antigravity":
        full = path.with_name("transcript_full.jsonl")
        if full.is_file():
            target = full
    last: str | None = None
    for record in _read_records(target):
        if agent == "claude":
            if record.get("type") != "assistant" or record.get("isSidechain"):
                continue
            texts = [
                t
                for t in _texts((record.get("message") or {}).get("content"))
                if t.strip()
            ]
            if texts:
                last = "\n".join(texts)
        elif agent == "antigravity":
            if record.get("type") == "PLANNER_RESPONSE":
                content = record.get("content")
                if isinstance(content, str) and content.strip():
                    last = content
        else:
            payload = (
                record.get("payload") if isinstance(record.get("payload"), dict) else {}
            )
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = "\n".join(_texts(payload.get("content")))
                if text.strip():
                    last = text
            elif payload.get("type") == "agent_message" and payload.get("message"):
                last = str(payload["message"])
            elif payload.get("type") == "task_complete" and payload.get(
                "last_agent_message"
            ):
                last = str(payload["last_agent_message"])
    if not last:
        return None
    cleaned = redact(last.strip())
    return (
        cleaned
        if len(cleaned) <= REPORT_TEXT_LIMIT
        else cleaned[: REPORT_TEXT_LIMIT - 1].rstrip() + "…"
    )


def latest_reports(
    *,
    agent: str = "all",
    hours: float = 168,
    now: float | None = None,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    antigravity_root: Path | None = None,
    events_root: Path | None = None,
) -> dict[str, Any]:
    """Latest assistant report/output for each requested coding agent."""
    now = time.time() if now is None else now
    events = load_events(events_root or events_dir())
    since = now - min(max(hours, 1.0), 720.0) * 3600
    wanted = ("claude", "codex", "antigravity") if agent == "all" else (agent,)
    parsers = {
        "claude": parse_claude,
        "codex": parse_codex,
        "antigravity": parse_antigravity,
    }
    names = {
        "claude": "Claude Code",
        "codex": "Codex",
        "antigravity": "Antigravity",
    }
    results: dict[str, dict[str, Any] | None] = {}
    for kind in wanted:
        paths: list[Path] = []
        if kind == "claude":
            paths = list(((claude_root or claude_dir()) / "projects").glob("*/*.jsonl"))
        elif kind == "codex":
            paths = list(
                ((codex_root or codex_dir()) / "sessions").glob("*/*/*/*.jsonl")
            )
        elif kind == "antigravity" and (
            antigravity_root is not None
            or (claude_root is None and codex_root is None)
        ):
            seen: set[Path] = set()
            for root in antigravity_roots(antigravity_root):
                for p in root.glob("*/.system_generated/logs/transcript.jsonl"):
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
        picked: dict[str, Any] | None = None
        for path in _recent(paths, since)[:5]:
            try:
                text = _full_last_message(kind, path)
                if not text:
                    continue
                session = parsers[kind](path, now, _event_for(kind, path, events))
            except (OSError, ValueError):
                continue
            if session is None:
                continue
            picked = {
                "agent": kind,
                "agent_name": names[kind],
                "project": session.project,
                "source": session.source,
                "title": session.title,
                "state": session.state,
                "minutes_ago": session.minutes_ago,
                "request": session.request,
                "files_changed": session.files_changed,
                "report": text,
            }
            break
        results[kind] = picked

    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "hours": hours,
        "reports": results,
    }
