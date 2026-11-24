"""Agent events: "Claude Code finished", "Codex is waiting for approval".

The PC's outbox sends each event over its WebSocket; the hub stores it, then
acknowledges it, so the PC only forgets an event the database already holds. The
master phone gets new events immediately through the event stream and catches up
with a cursor after any gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

SCHEMA_VERSION = 1
EVENT_KINDS = ("agent_finished", "agent_waiting")
EVENT_STATUSES = (
    "turn_completed",  # the agent stopped answering; not proof the task is done
    "completed",
    "failed",
    "interrupted",
    "approval",
    "question",
)
TEXT_LIMITS = {
    "agent": 30,
    "host": 40,
    "project": 120,
    "session_id": 120,
    "turn_id": 120,
    "title": 160,
    "summary": 600,
    "report": 20_000,
}
DATA_LIMIT = 8_000
_ID = re.compile(r"[A-Za-z0-9_-]{8,80}")


class EventError(ValueError):
    pass


def validate_event(raw: Any) -> dict[str, Any]:
    """Event fields a PC may set, trimmed to their limits."""
    if not isinstance(raw, dict):
        raise EventError("event must be an object")
    event_id = str(raw.get("event_id") or "")
    if not _ID.fullmatch(event_id):
        raise EventError("event_id must be 8-80 letters, digits, - or _")
    kind = raw.get("kind")
    if kind not in EVENT_KINDS:
        raise EventError(f"kind must be one of {EVENT_KINDS}")
    status = raw.get("status")
    if status is not None and status not in EVENT_STATUSES:
        raise EventError(f"status must be one of {EVENT_STATUSES}")
    event: dict[str, Any] = {"id": event_id, "kind": kind, "status": status}
    for name, limit in TEXT_LIMITS.items():
        value = raw.get(name)
        event[name] = str(value)[:limit] if value not in (None, "") else None
    if not event["title"]:
        raise EventError("title is required")
    for name in ("started_at", "finished_at"):
        try:
            event[name] = float(raw[name]) if raw.get(name) is not None else None
        except (TypeError, ValueError):
            raise EventError(f"{name} must be a number") from None
    data = raw.get("data")
    if data is not None and not isinstance(data, dict):
        raise EventError("data must be an object")
    if data and len(str(data)) > DATA_LIMIT:
        raise EventError("data is too large")
    event["data"] = data or None
    return event


class EventBus:
    """Fan-out of newly stored events to the open phone streams."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            # A stuck stream misses live pushes and recovers from its cursor.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
