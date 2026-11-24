"""Hub for the user's Windows PCs running jarvis_desktop.

Each PC keeps one outbound WebSocket open. The agent asks for an action through the
internal API; the hub forwards it to a PC that announced that capability and waits
for the result. Actions are short-lived: they are never queued for an offline PC and
never re-sent after a disconnect, so a song cannot start hours later.

In the other direction the PC sends agent events from its durable outbox. Each one
is acknowledged only after it is stored, so the PC can retry until it is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

from .events import EventBus, EventError, validate_event
from .store import Device, Store

logger = logging.getLogger("jarvis_control")


class DesktopOfflineError(Exception):
    pass


class DesktopTimeoutError(Exception):
    pass


@dataclass
class _Connection:
    device: Device
    ws: web.WebSocketResponse
    connected_at: float
    capabilities: list[str] = field(default_factory=list)
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    outbox: dict[str, Any] | None = None


class DesktopHub:
    def __init__(
        self,
        store: Store,
        *,
        bus: EventBus | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self.bus = bus or EventBus()
        self._clock = clock
        self._connections: dict[str, _Connection] = {}
        # Results that arrive after the caller stopped waiting (cancel, timeout).
        self.late_result: Callable[[str, dict[str, Any]], None] | None = None
        # Results of the coding agents' jobs (the orchestrator stores them).
        self.job_update: Callable[[dict[str, Any]], None] | None = None
        self._action_conn: dict[str, _Connection] = {}

    def _pick(self, action: str | None = None) -> _Connection | None:
        candidates = [
            conn
            for conn in self._connections.values()
            if not conn.ws.closed and (action is None or action in conn.capabilities)
        ]
        return max(candidates, key=lambda conn: conn.connected_at, default=None)

    def status(self) -> dict[str, Any]:
        conn = self._pick()
        return {
            "online": conn is not None,
            "device": conn.device.name if conn else None,
            "capabilities": list(conn.capabilities) if conn else [],
            # Pending events and saturation, as last reported by the PC's outbox.
            "outbox": conn.outbox if conn else None,
        }

    def _store_event(self, conn: _Connection, raw: Any) -> dict[str, Any]:
        """Store an agent event and build its acknowledgement."""
        event_id = str(raw.get("event_id", "")) if isinstance(raw, dict) else ""
        try:
            event = validate_event(raw)
        except EventError as exc:
            # Rejected for good: the PC drops it instead of retrying forever.
            logger.warning("rejected event from %s: %s", conn.device.name, exc)
            return {
                "type": "agent_event_ack",
                "event_id": event_id,
                "ok": False,
                "error": str(exc),
            }
        row, created = self._store.add_event(conn.device.id, event, self._clock())
        if created:
            logger.info("event %s: %s", event["kind"], event["title"])
            self.bus.publish(row)
        return {"type": "agent_event_ack", "event_id": event["id"], "ok": True}

    async def call(
        self,
        action: str,
        params: dict[str, Any],
        timeout: float,
        *,
        on_sent: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        conn = self._pick(action)
        if conn is None:
            raise DesktopOfflineError
        now = self._clock()
        action_id = self._store.add_action(conn.device.id, action, now, now + timeout)
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        conn.pending[action_id] = future
        self._action_conn[action_id] = conn
        try:
            await conn.ws.send_json(
                {
                    "type": "action",
                    "id": action_id,
                    "action": action,
                    "params": params,
                    "ttl": timeout,
                }
            )
            if on_sent is not None:
                on_sent(action_id)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._store.finish_action(action_id, "expired", self._clock())
            # Not waiting any more does not stop the PC: ask it to.
            await self.cancel_action(action_id)
            raise DesktopTimeoutError from None
        except asyncio.CancelledError:
            self._store.finish_action(action_id, "cancelled", self._clock())
            await asyncio.shield(self.cancel_action(action_id))
            raise
        except (DesktopOfflineError, ConnectionError):
            self._store.finish_action(action_id, "unknown", self._clock())
            raise DesktopOfflineError from None
        finally:
            conn.pending.pop(action_id, None)

    async def cancel_action(self, action_id: str) -> bool:
        """Tell the PC to stop an action; False when it cannot be reached."""
        conn = self._action_conn.get(action_id)
        if conn is None or conn.ws.closed:
            return False
        try:
            await conn.ws.send_json({"type": "cancel", "id": action_id})
        except (ConnectionError, RuntimeError):
            return False
        return True

    def _resolve(self, conn: _Connection, data: dict[str, Any]) -> None:
        action_id = str(data.get("id", ""))
        ok = bool(data.get("ok"))
        self._store.finish_action(
            action_id,
            "done" if ok else "failed",
            self._clock(),
            data.get("result") if ok else data.get("error"),
        )
        future = conn.pending.get(action_id)
        if future is not None and not future.done():
            future.set_result(data)
        elif self.late_result is not None:
            self.late_result(action_id, data)
        self._action_conn.pop(action_id, None)

    async def serve(
        self, request: web.Request, device: Device
    ) -> web.WebSocketResponse:
        # Screenshots from the PC are a few hundred KB of base64.
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8 << 20)
        await ws.prepare(request)
        conn = _Connection(device, ws, self._clock())
        previous = self._connections.get(device.id)
        self._connections[device.id] = conn
        if previous is not None:
            await previous.ws.close(code=4000, message=b"replaced by a new connection")
        logger.info("desktop %s connected", device.name)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if data.get("type") == "hello":
                    conn.capabilities = [str(c) for c in data.get("capabilities", [])][
                        :50
                    ]
                    for result in data.get("results", [])[:100]:
                        self._resolve(conn, result)
                    logger.info("desktop %s offers %s", device.name, conn.capabilities)
                elif data.get("type") == "notify":
                    self._store.add_notification(
                        str(data.get("kind", "info"))[:30],
                        str(data.get("title", "JARVIS")),
                        str(data.get("body", "")),
                        self._clock(),
                    )
                elif data.get("type") == "result":
                    self._resolve(conn, data)
                elif data.get("type") == "agent_event":
                    ack = self._store_event(conn, data.get("event"))
                    await ws.send_json(ack)
                elif data.get("type") in {"job_update", "job_progress"} and isinstance(
                    data.get("job"), dict
                ):
                    if self.job_update is not None:
                        self.job_update(
                            data["job"]
                        )  # stored before the acknowledgement
                        if data["type"] == "job_progress":
                            continue
                        await ws.send_json(
                            {
                                "type": "job_ack",
                                "job_id": str(data["job"].get("id", "")),
                            }
                        )
                elif data.get("type") == "outbox_status":
                    conn.outbox = {
                        "pending": int(data.get("pending") or 0),
                        "oldest_at": data.get("oldest_at"),
                        "saturated": bool(data.get("saturated")),
                        "reported_at": self._clock(),
                    }
        finally:
            if self._connections.get(device.id) is conn:
                del self._connections[device.id]
            for future in conn.pending.values():
                if not future.done():
                    future.set_exception(DesktopOfflineError())
            logger.info("desktop %s disconnected", device.name)
        return ws
