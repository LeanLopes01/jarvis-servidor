"""Durable outbox of agent events written by agent_hook.py.

Each event is a file in %LOCALAPPDATA%/JarvisDesktop/outbox. The helper completes it
once from the session transcript (final answer, request, files changed, how the turn
ended), redacts secrets, sends it, and deletes the file only when the server's
acknowledgement says it was stored. Unsent events survive restarts; there is no
in-memory cap that silently drops them.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path, PurePath
from typing import Any

from .agent_hook import outbox_dir
from .coding import (
    _read_records,
    _texts,
    clip,
    parse_antigravity,
    parse_claude,
    parse_codex,
    redact,
)

logger = logging.getLogger("jarvis_desktop")

AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
# CLAUDE_CODE_ENTRYPOINT values and transcript originators, as the phone shows them.
HOST_NAMES = {
    "cli": "CLI",
    "claude-vscode": "VS Code",
    "vscode": "VS Code",
    "codex_vscode": "VS Code",
    "codex_cli_rs": "CLI",
    "sdk-ts": "Agent SDK",
    "sdk-py": "Agent SDK",
    "sdk-cli": "Agent SDK",
    "claude-desktop": "Claude Desktop",
    "Antigravity": "Antigravity",
}
KEEP_DAYS = 7
SATURATION = 500  # pending events before the server is told the outbox is backed up
RESEND_SECONDS = 30.0
SUMMARY_LIMIT = 300
REPORT_LIMIT = 20_000
SCHEMA_VERSION = 1


def final_message(agent: str, path: Path) -> str | None:
    """The agent's last answer in full, from the end of its transcript."""
    last = None
    for record in _read_records(path):
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
                last = "\n".join(_texts(payload.get("content"))) or last
            elif payload.get("type") == "agent_message" and payload.get("message"):
                last = payload["message"]
            elif payload.get("type") == "task_complete" and payload.get(
                "last_agent_message"
            ):
                last = payload["last_agent_message"]
    return last


def _report_text(text: str | None) -> str | None:
    if not text:
        return None
    text = redact(text.strip())
    return text if len(text) <= REPORT_LIMIT else text[: REPORT_LIMIT - 1] + "…"


def build_event(entry: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The event the server stores, from an outbox entry and its transcript."""
    now = time.time() if now is None else now
    agent = str(entry.get("agent") or "")
    name = AGENT_NAMES.get(agent, "A coding agent")
    cwd = str(entry.get("cwd") or "")
    project = PurePath(cwd.replace("\\", "/")).name or None
    status = entry.get("status")
    host = entry.get("host") or "unknown"
    message = entry.get("message")
    data: dict[str, Any] = {}

    transcript = entry.get("transcript_path")
    path = Path(transcript) if transcript else None
    if path is not None and path.is_file():
        try:
            parsers = {
                "claude": parse_claude,
                "antigravity": parse_antigravity,
            }
            parser = parsers.get(agent, parse_codex)
            session = parser(path, now, None)
            if entry.get("kind") == "agent_finished":
                message = message or final_message(agent, path)
        except (OSError, ValueError):
            session = None
        if session is not None:
            project = project or session.project
            # The hook's environment is the better source; the transcript fills gaps.
            if host == "unknown" and session.source:
                host = session.source
            if session.state == "interrupted" and entry.get("kind") == "agent_finished":
                status = "interrupted"
            data["request"] = session.request
            data["files_changed"] = session.files_changed
            data["title"] = session.title
    if entry.get("continued"):
        data["continued_after_hook"] = True
    if entry.get("kind") == "agent_waiting":
        data["tool"] = entry.get("tool")
        data["detail"] = clip(entry.get("detail"), 160)

    project_label = project or "a project"
    if entry.get("kind") == "agent_waiting":
        if status == "question":
            title = f"{name} has a question · {project_label}"
            summary = "It is waiting for your answer."
        else:
            tool = entry.get("tool") or "a tool"
            title = f"{name} needs approval · {project_label}"
            summary = f"It is waiting for you to approve {tool}."
        report = None
    else:
        verb = "was interrupted" if status == "interrupted" else "finished"
        title = f"{name} {verb} · {project_label}"
        summary = clip(message, SUMMARY_LIMIT) or "It stopped without a final message."
        report = _report_text(message)
    return {
        "event_id": entry["event_id"],
        "kind": entry["kind"],
        "status": status,
        "agent": agent,
        "host": HOST_NAMES.get(str(host), str(host))[:40],
        "project": project,
        "session_id": entry.get("session_id"),
        "turn_id": entry.get("turn_id"),
        "title": title,
        "summary": summary,
        "report": report,
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
        "data": {k: v for k, v in data.items() if v not in (None, [], "")},
    }


class Outbox:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or outbox_dir()
        self._sent_at: dict[str, float] = {}
        self.expired = 0

    def _files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(self.dir.glob("*.json"))

    def _rewrite(self, path: Path, payload: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            "w", dir=self.dir, delete=False, suffix=".tmp", encoding="utf-8"
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
        os.replace(tmp.name, path)

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Events to send now: never sent, or sent without an answer for a while."""
        now = time.time() if now is None else now
        messages = []
        for path in self._files():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # still being written, or broken: looked at again later
            if now - float(entry.get("recorded_at") or now) > KEEP_DAYS * 86400:
                logger.warning(
                    "dropping event older than %d days: %s", KEEP_DAYS, path.name
                )
                path.unlink(missing_ok=True)
                self.expired += 1
                continue
            event_id = str(entry.get("event_id") or "")
            if not event_id or now - self._sent_at.get(event_id, 0) < RESEND_SECONDS:
                continue
            if "event" not in entry:
                # Completed once, then kept: a retry sends exactly the same event.
                entry["event"] = build_event(entry, now)
                self._rewrite(path, entry)
            self._sent_at[event_id] = now
            messages.append(
                {
                    "type": "agent_event",
                    "schema_version": SCHEMA_VERSION,
                    "event": entry["event"],
                }
            )
        return messages

    def acknowledge(self, ack: dict[str, Any]) -> bool:
        """Forget an event the server stored (or rejected for good)."""
        event_id = str(ack.get("event_id") or "")
        if not event_id:
            return False
        if not ack.get("ok"):
            logger.warning("server rejected event %s: %s", event_id, ack.get("error"))
        self._sent_at.pop(event_id, None)
        for path in self.dir.glob(f"*-{event_id}.json"):
            path.unlink(missing_ok=True)
            return True
        return False

    def status(self) -> dict[str, Any]:
        files = self._files()
        oldest = None
        if files:
            try:
                oldest = int(files[0].name.split("-", 1)[0]) / 1000
            except ValueError:
                oldest = None
        return {
            "type": "outbox_status",
            "pending": len(files),
            "oldest_at": oldest,
            "saturated": len(files) >= SATURATION,
            "expired": self.expired,
        }

    def forget_inflight(self) -> None:
        """After a disconnect: send everything again on the next connection."""
        self._sent_at.clear()
