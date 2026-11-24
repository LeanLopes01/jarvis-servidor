"""Keeps the voice conversation as it happens, for JARVIS's own diagnostics reports.

Session callbacks only put small records in a queue; a background task sends them
to jarvis-control's internal API every second, so nothing here waits on the audio
path. Records: what the user said (as transcribed), what Jarvis said, tool calls
with their results, agent state changes and errors. No audio is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger("agent")

FLUSH_SECONDS = 1.0
QUEUE_LIMIT = 2000


# Personal data (mail, files, calendars, tasks) stays out of the kept transcript.
PRIVATE_TOOLS = frozenset(
    {
        "calendar_list_events",
        "drive_search",
        "drive_read",
        "mail_search",
        "mail_read",
        "notion_list_tasks",
    }
)


def _private(output: Any) -> dict[str, Any]:
    """Only the outcome of a personal-data call: counts and error codes."""
    summary: dict[str, Any] = {
        "is_error": bool(getattr(output, "is_error", False)) if output else None,
        "content": "omitted",
    }
    try:
        body = json.loads(getattr(output, "output", "") or "{}")
    except (TypeError, ValueError):
        return summary
    if isinstance(body, dict):
        summary["items"] = len(body.get("items") or [])
        summary["partial"] = bool(body.get("partial"))
        summary["errors"] = [
            str(e.get("code")) for e in body.get("errors") or [] if isinstance(e, dict)
        ][:6]
    return summary


def _trim(value: Any, limit: int) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ConversationLog:
    def __init__(self, base_url: str, token: str, room: str) -> None:
        self._url = base_url.rstrip("/") + "/internal/v1/conversation"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._room = room
        self._queue: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def add(
        self,
        kind: str,
        *,
        role: str | None = None,
        text: str | None = None,
        data: Any = None,
    ) -> None:
        if len(self._queue) >= QUEUE_LIMIT:
            self._queue.pop(0)  # a stuck server must not grow memory without end
        self._queue.append(
            {
                "room": self._room,
                "kind": kind,
                "role": role,
                "text": text,
                "data": data,
                "at": time.time(),
            }
        )

    def attach(self, session: Any) -> None:
        @session.on("conversation_item_added")
        def _item(ev: Any) -> None:
            item = ev.item
            text = getattr(item, "text_content", None)
            if text:
                self.add(
                    "message",
                    role=getattr(item, "role", None),
                    text=text,
                    data={"interrupted": getattr(item, "interrupted", False)},
                )

        @session.on("function_tools_executed")
        def _tools(ev: Any) -> None:
            for call, output in zip(
                ev.function_calls, ev.function_call_outputs, strict=False
            ):
                if getattr(call, "name", None) in PRIVATE_TOOLS:
                    self.add("tool", role="tool", text=call.name, data=_private(output))
                    continue
                self.add(
                    "tool",
                    role="tool",
                    text=getattr(call, "name", "tool"),
                    data={
                        "arguments": _trim(getattr(call, "arguments", ""), 600),
                        "output": _trim(
                            getattr(output, "output", "") if output else "", 2000
                        ),
                        "is_error": bool(getattr(output, "is_error", False))
                        if output
                        else None,
                    },
                )

        @session.on("agent_state_changed")
        def _state(ev: Any) -> None:
            self.add("state", role="agent", text=f"{ev.old_state} -> {ev.new_state}")

        @session.on("error")
        def _error(ev: Any) -> None:
            self.add(
                "error",
                text=_trim(repr(ev.error), 1000),
                data={"source": type(ev.source).__name__},
            )

        @session.on("close")
        def _close(ev: Any) -> None:
            self.add(
                "state",
                text=f"session closed: {ev.reason}",
                data={"error": repr(ev.error) if ev.error else None},
            )

        self._task = asyncio.ensure_future(self._pump())

    async def _send(self, http: aiohttp.ClientSession) -> None:
        if not self._queue:
            return
        batch, self._queue = self._queue[:200], self._queue[200:]
        try:
            async with http.post(
                self._url, json={"items": batch}, headers=self._headers
            ) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientError(f"HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.debug("conversation log kept for later: %s", exc)
            self._queue = batch + self._queue  # retry with the next flush

    async def _pump(self) -> None:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            while not self._closed:
                await asyncio.sleep(FLUSH_SECONDS)
                await self._send(http)
            await self._send(http)

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 6)
