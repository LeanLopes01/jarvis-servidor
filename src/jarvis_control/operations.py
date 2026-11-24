"""What the user asked for and where it stands: the answer to "Jarvis, what are you
doing?" and the handle for "stop what you are doing".

Every command from the phone becomes an operation with a stage ("waiting for the
music queue", "sent to the computer"), a deadline and, while it runs, an asyncio
task. Reading the state never touches the music queue or the PC, so a stuck
Spotify command cannot block a status question. Cancelling stops the waiting task
and tells the PC to stop the action; the operation says "cancelled" only once the
PC confirms, and "completed_before_cancel" when the action had already finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from .events import EventBus
from .store import Store

logger = logging.getLogger("jarvis_control")

ACTIVE = ("accepted", "queued", "running", "cancelling")
FINAL = (
    "completed",
    "failed",
    "cancelled",
    "completed_before_cancel",
    "cancellation_unconfirmed",
    "stalled",
    "expired",
)
STALL_GRACE = 10.0  # past the deadline before the supervisor steps in
UNCONFIRMED_AFTER = 10.0  # waiting for the PC to confirm a cancel


class Operations:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._bus = bus
        self._clock = clock
        self._ops: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._by_action: dict[str, str] = {}
        self.cancel_action: Callable[[str], Any] | None = None  # set by the hub
        # After a restart nothing from before is still running here.
        for op in store.unfinished_operations():
            op.update(
                state="cancellation_unconfirmed"
                if op["cancel_requested_at"]
                else "stalled",
                stage="jarvis-control restarted while it was running",
                finished_at=self._clock(),
                updated_at=self._clock(),
            )
            store.save_operation(op)

    def start(
        self,
        device_id: str,
        kind: str,
        label: str,
        *,
        op_id: str | None = None,
        deadline: float | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        op = {
            "id": op_id or uuid.uuid4().hex,
            "device_id": device_id,
            "kind": kind,
            "label": label[:200],
            "state": "accepted",
            "stage": "accepted",
            "host": host,
            "created_at": now,
            "updated_at": now,
            "deadline": deadline,
            "cancel_requested_at": None,
            "finished_at": None,
            "action_id": None,
            "result": None,
        }
        self._ops[op["id"]] = op
        self._store.save_operation(op)
        return op

    def update(self, op_id: str, **fields: Any) -> None:
        op = self._ops.get(op_id)
        if op is None or op["finished_at"] is not None:
            return
        if fields.get("action_id"):
            self._by_action[fields["action_id"]] = op_id
        op.update(fields, updated_at=self._clock())
        self._store.save_operation(op)

    def attach(self, op_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks[op_id] = task

    def finish(
        self, op_id: str, state: str, result: Any = None
    ) -> dict[str, Any] | None:
        op = self._ops.get(op_id)
        if op is None or op["finished_at"] is not None:
            return op
        if op["cancel_requested_at"] and state == "completed":
            state = "completed_before_cancel"  # the effect happened anyway
        now = self._clock()
        op.update(state=state, finished_at=now, updated_at=now, result=result)
        self._store.save_operation(op)
        self._tasks.pop(op_id, None)
        return op

    def get(self, op_id: str) -> dict[str, Any] | None:
        return self._ops.get(op_id)

    def active(self, device_id: str | None = None) -> list[dict[str, Any]]:
        return [
            op
            for op in self._ops.values()
            if op["finished_at"] is None
            and (device_id is None or op["device_id"] == device_id)
        ]

    async def cancel(self, device_id: str, op_id: str) -> dict[str, Any] | None:
        """Ask to stop one operation. Returns its state right after asking."""
        op = self._ops.get(op_id)
        if op is None or op["device_id"] != device_id:
            return None
        if op["finished_at"] is not None:
            return op
        now = self._clock()
        op.update(state="cancelling", cancel_requested_at=now, updated_at=now)
        self._store.save_operation(op)
        if op.get("action_id") and self.cancel_action is not None:
            with contextlib.suppress(Exception):
                await self.cancel_action(op["action_id"])
        task = self._tasks.get(op_id)
        if task is not None and not task.done():
            task.cancel()
        asyncio.get_running_loop().call_later(
            UNCONFIRMED_AFTER, self._unconfirmed, op_id
        )
        return op

    def _unconfirmed(self, op_id: str) -> None:
        op = self._ops.get(op_id)
        if op is not None and op["finished_at"] is None:
            self.finish(
                op_id,
                "cancellation_unconfirmed",
                {"message": "The computer did not confirm the stop."},
            )

    async def cancel_all(self, device_id: str) -> list[dict[str, Any]]:
        return [
            op
            for op in [
                await self.cancel(device_id, o["id"]) for o in self.active(device_id)
            ]
            if op is not None
        ]

    def action_result(self, action_id: str, data: dict[str, Any]) -> None:
        """A PC result for an action whose operation was cancelled or timed out."""
        op_id = self._by_action.pop(action_id, None)
        op = self._ops.get(op_id or "")
        if op is None:
            return
        if op["finished_at"] is not None:
            # Record what really happened after we stopped waiting.
            late = (
                "completed"
                if data.get("ok")
                else ("cancelled" if data.get("cancelled") else "failed")
            )
            op["result"] = {**(op.get("result") or {}), "late_result": late}
            self._store.save_operation(op)
            return
        if data.get("cancelled"):
            self.finish(op["id"], "cancelled", {"message": "Stopped on the computer."})

    def runtime(self, device_id: str) -> dict[str, Any]:
        now = self._clock()
        active = [
            {
                **_view(op, now),
                "overdue": bool(op["deadline"] and now > op["deadline"]),
            }
            for op in self.active(device_id)
        ]
        recent = [
            _view(op, now)
            for op in self._store.operations(
                device_id=device_id, since=now - 3600, limit=5
            )
        ]
        return {"active": active, "recent": recent}

    async def tick(self) -> None:
        """Mark operations that outlived their deadline and stop what is left of them."""
        now = self._clock()
        for op in self.active():
            if op["deadline"] and now > op["deadline"] + STALL_GRACE:
                logger.warning("operation %s stalled at %s", op["id"][:8], op["stage"])
                task = self._tasks.get(op["id"])
                if task is not None and not task.done():
                    task.cancel()
                self.finish(op["id"], "stalled", {"stage": op["stage"]})
                row = self._store.add_server_event(
                    {
                        "id": f"stall-{op['id']}"[:80],
                        "kind": "operation_stalled",
                        "status": "failed",
                        "title": f"Stalled: {op['label']}"[:160],
                        "summary": f"No answer at the stage: {op['stage']}."[:600],
                        "data": {"operation_id": op["id"]},
                    },
                    now,
                )
                self._bus.publish(row)
        # Keep only what may still be asked about.
        for op_id in [
            k
            for k, op in self._ops.items()
            if op["finished_at"] and now - op["finished_at"] > 3600
        ]:
            self._ops.pop(op_id, None)


def _view(op: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "id": op["id"],
        "kind": op["kind"],
        "label": op["label"],
        "state": op["state"],
        "stage": op["stage"],
        "host": op["host"],
        "seconds": round((op["finished_at"] or now) - op["created_at"], 1),
        "updated_seconds_ago": round(now - op["updated_at"], 1),
        "cancel_requested": op["cancel_requested_at"] is not None,
        "result": op.get("result"),
    }
