"""Live telemetry from the coding CLIs' JSON streams, for the orchestrator's monitor.

Formats confirmed against the installed versions (22/10/2026):
  Claude Code 2.1  -p --output-format stream-json --verbose: system/assistant/user
                   events, the final "result" object on the last line.
  Codex 0.163      exec --json: thread/turn/item events, usage on turn.completed.
  Antigravity 1.4  -p --output-format stream-json: step_update events, the final
                   result wrapped as {"event": "result", "result": {...}}.

Only public signals leave the PC: tool calls, the agent's own messages and the
reasoning summaries Codex publishes. Claude's thinking blocks are never read.
Everything is redacted and bounded (100 events, 2,000 characters each).
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from .coding import redact

MAX_EVENTS = 100
EVENT_TEXT = 2000
SUMMARY_TEXT = 6000
ACTIVITY_TEXT = 300


def _short(text: Any, limit: int) -> str:
    text = redact(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tool_detail(name: str, params: Any) -> str:
    """The one argument that says what a tool is doing (file, pattern or command)."""
    if not isinstance(params, dict):
        return name
    for key in (
        "file_path",
        "path",
        "pattern",
        "command",
        "CommandLine",
        "query",
        "url",
        "AbsolutePath",
        "SearchPath",
        "TargetFile",
    ):
        if params.get(key):
            return f"{name}: {params[key]}"
    return name


class StreamTelemetry:
    """Fed line by line with a CLI's stdout; `snapshot()` is the progress body."""

    def __init__(self, job_id: str, agent: str, *, clock=time.time) -> None:
        self.job_id = job_id
        self.agent = agent
        self._clock = clock
        self._seq = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.activity: str | None = None
        self.reasoning: list[str] = []
        self.usage: dict[str, Any] = {}
        self.last_output_at: float | None = None
        self.denied: list[str] = []  # tool calls refused by a permission check
        self._buffer = b""
        self._claude_messages: dict[str, dict[str, int]] = {}
        self._agy_text: dict[int, str] = {}
        self._agy_usage: dict[int, dict[str, int]] = {}

    # Input

    def feed(self, chunk: bytes) -> None:
        self.last_output_at = self._clock()
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                handler = getattr(self, f"_{self.agent}", None)
                if handler is not None:
                    handler(data)

    def _event(self, kind: str, text: str, *, activity: bool = True) -> None:
        text = _short(text, EVENT_TEXT)
        if not text:
            return
        self._seq += 1
        self.events.append(
            {
                "id": f"{self.job_id}:{self._seq}",
                "at": self._clock(),
                "kind": kind,
                "text": text,
            }
        )
        if activity:
            self.activity = _short(text, ACTIVITY_TEXT)

    # Claude Code

    def _claude(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            if data.get("model"):
                self.usage["model"] = str(data["model"])[:100]
            self._event("status", "Sessão do Claude Code iniciada")
        elif kind == "assistant":
            message = data.get("message") or {}
            if message.get("model"):
                self.usage["model"] = str(message["model"])[:100]
            usage = message.get("usage") or {}
            if message.get("id") and usage:
                # The same message repeats once per content block: count it once.
                self._claude_messages[message["id"]] = {
                    "input": int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0),
                }
                self._sum_claude()
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    self._event(
                        "tool", _tool_detail(str(block.get("name")), block.get("input"))
                    )
                elif block.get("type") == "text":
                    self._event("message", block.get("text"))
        elif kind == "user":
            for block in (data.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("is_error"):
                    self._event(
                        "error",
                        f"Ferramenta falhou: {block.get('content')}",
                        activity=False,
                    )
        elif kind == "result":
            usage = data.get("usage") or {}
            if usage:
                self.usage.update(
                    input_tokens=int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    source="claude-cli",
                )
            self._event(
                "status",
                f"Claude Code terminou ({data.get('subtype') or 'sem status'})",
            )

    def _sum_claude(self) -> None:
        self.usage.update(
            input_tokens=sum(m["input"] for m in self._claude_messages.values()),
            output_tokens=sum(m["output"] for m in self._claude_messages.values()),
            source="claude-cli",
        )

    # Codex

    def _codex(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        item = data.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            self._event("status", "Sessão do Codex iniciada")
        elif kind == "item.started" and itype == "command_execution":
            self._event("tool", f"Comando: {item.get('command')}")
        elif kind == "item.completed":
            if itype == "agent_message":
                self._event("message", item.get("text"))
            elif itype == "reasoning":
                # Codex publishes reasoning summaries (not hidden chain of thought).
                self.reasoning.append(_short(item.get("text"), EVENT_TEXT))
            elif itype == "command_execution":
                self._event(
                    "tool",
                    f"Comando terminou (código {item.get('exit_code')}): {item.get('command')}",
                    activity=False,
                )
            elif itype == "file_change":
                paths = [
                    c.get("path")
                    for c in item.get("changes") or []
                    if isinstance(c, dict)
                ]
                self._event(
                    "tool", "Arquivos alterados: " + ", ".join(map(str, paths[:10]))
                )
            elif itype:
                self._event("tool", str(itype))
        elif kind == "turn.completed":
            usage = data.get("usage") or {}
            self.usage["input_tokens"] = self.usage.get("input_tokens", 0) + int(
                usage.get("input_tokens") or 0
            )
            self.usage["output_tokens"] = self.usage.get("output_tokens", 0) + int(
                usage.get("output_tokens") or 0
            )
            self.usage["source"] = "codex-cli"
        elif kind in {"turn.failed", "error"}:
            error = data.get("error") or data.get("message") or data
            self._event("error", f"Codex informou erro: {error}")

    # Antigravity

    def _antigravity(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if event == "init":
            self._event("status", "Sessão do Antigravity iniciada")
            return
        if event == "result":
            result = data.get("result") or {}
            self._agy_final_usage(result.get("usage"))
            denied = result.get("denied_actions") or []
            if denied:
                names = ", ".join(
                    str(d.get("display_name") or d.get("action")) for d in denied
                )
                self._event("error", f"Permissão negada no modo headless: {names}")
            self._event(
                "status",
                f"Antigravity terminou ({result.get('status') or 'sem status'})",
            )
            return
        step = data.get("step_update") or {}
        index = int(step.get("step_index") or 0)
        state = step.get("state")
        stype = step.get("step_type")
        if stype == "tool":
            detail = _tool_detail(
                str(step.get("tool_name")),
                (step.get("tool_info") or {}).get("parameters"),
            )
            if state == "ACTIVE":
                self._event("tool", detail)
            elif state == "ERROR":
                error = ((step.get("tool_info") or {}).get("error") or {}).get(
                    "message"
                )
                self._event("error", f"{detail} falhou: {error}")
                if "permission" in str(error).lower():
                    self.denied.append(_short(detail, 500))
        elif stype == "agent_response":
            if step.get("text_delta"):
                self._agy_text[index] = self._agy_text.get(index, "") + str(
                    step["text_delta"]
                )
            if step.get("usage"):
                usage = step["usage"]
                self._agy_usage[index] = {
                    "input": int(usage.get("input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0),
                }
                self.usage.update(
                    input_tokens=sum(u["input"] for u in self._agy_usage.values()),
                    output_tokens=sum(u["output"] for u in self._agy_usage.values()),
                    source="antigravity-cli",
                )
            if state == "DONE" and self._agy_text.get(index, "").strip():
                self._event("message", self._agy_text.pop(index))

    def _agy_final_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.usage.update(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                source="antigravity-cli",
            )

    # Output

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Optional fields only when known: absence means unknown, never zero."""
        now = self._clock() if now is None else now
        body: dict[str, Any] = {"events": list(self.events)}
        activity = self.activity
        if self.last_output_at is None:
            activity = "Processo iniciado; aguardando o primeiro evento do CLI"
        elif now - self.last_output_at >= 60:
            silent = int(now - self.last_output_at)
            activity = f"{activity or 'Trabalhando'} · sem novos eventos há {silent} s"
        if activity:
            body["activity"] = activity
        if self.last_output_at is not None:
            body["last_output_at"] = self.last_output_at
        if self.reasoning:
            summary = "\n".join(self.reasoning)
            body["reasoning_summary"] = summary[-SUMMARY_TEXT:]
        if self.usage.get("source"):
            body["usage"] = dict(self.usage)
        return body


def final_line(stdout: str) -> dict[str, Any]:
    """The CLI's final result object, from the old single-JSON or the stream format."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("event") == "result" and isinstance(data.get("result"), dict):
            return data["result"]  # Antigravity stream-json wraps its result
        return data
    return {}
