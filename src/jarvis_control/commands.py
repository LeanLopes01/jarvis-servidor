"""Structured commands from the phone that need no voice model, such as "pause".

Each command has an id chosen by the phone and runs at most once: a retry after a
lost reply gets the stored outcome. A command carries a deadline and is refused once
it passes, so a request made before a network drop cannot start music much later.

Music on the master phone always goes to the computer. The rule is applied here and
again by the PC, not only in the voice instructions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .desktop import DesktopHub, DesktopOfflineError, DesktopTimeoutError
from .operations import Operations
from .store import Store

MUSIC_KINDS = {"track", "artist", "album", "playlist"}
MUSIC_CONTROLS = {"pause", "resume", "next", "previous"}
TARGETS = ("computer", "phone")
DEFAULT_TTL = 30.0
MAX_TTL = 120.0
STATUS_TIMEOUT = 10.0
LABELS = {
    "music.play": "Playing {query} on the computer",
    "music.control": "Music: {action} on the computer",
    "music.volume": "Music volume to {percent}%",
}


@dataclass(frozen=True)
class Outcome:
    status: int
    body: dict[str, Any]


class PolicyError(Exception):
    pass


def music_target(store: Store, device_id: str, requested: Any) -> str:
    """Where this device's music may play. The master phone: only the computer."""
    forced = store.master_music_target(device_id)
    if forced is not None:
        if requested not in (None, "", forced):
            raise PolicyError("Music from the master phone plays only on the computer.")
        return forced
    target = str(requested or "computer")
    if target not in TARGETS:
        raise PolicyError("Unknown music device.")
    return target


def _desktop_params(
    kind: str, params: dict[str, Any], target: str
) -> tuple[str, dict[str, Any]]:
    if kind == "music.play":
        query = str(params.get("query", "")).strip()[:200]
        what = str(params.get("kind", "track"))
        if not query or what not in MUSIC_KINDS:
            raise ValueError(
                "Send a query and a kind (track, artist, album, playlist)."
            )
        return "spotify.play", {"query": query, "kind": what, "device": target}
    if kind == "music.control":
        action = str(params.get("action", ""))
        if action not in MUSIC_CONTROLS:
            raise ValueError("action must be pause, resume, next or previous.")
        return "spotify.control", {"action": action, "device": target}
    if kind == "music.volume":
        try:
            percent = int(params.get("percent"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("percent must be a number from 0 to 100.") from None
        return "spotify.volume", {
            "level": min(100, max(0, percent)) / 100,
            "device": target,
        }
    if kind == "music.status":
        return "spotify.now_playing", {"device": target}
    raise ValueError(f"Unknown command kind {kind!r}.")


class CommandRunner:
    def __init__(
        self,
        store: Store,
        hub: DesktopHub,
        operations: Operations | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._hub = hub
        self._clock = clock
        self.operations = operations or Operations(store, hub.bus, clock=clock)
        # One music command at a time, in arrival order. Status reads skip it.
        self._music = asyncio.Lock()

    async def run(self, device_id: str, body: dict[str, Any]) -> Outcome:
        now = self._clock()
        command_id = str(body.get("id") or "").strip()[:80]
        kind = str(body.get("kind") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if not command_id:
            return Outcome(400, {"error": "bad_request", "message": "Send an id."})
        try:
            deadline = float(body.get("deadline") or now + DEFAULT_TTL)
        except (TypeError, ValueError):
            return Outcome(400, {"error": "bad_request", "message": "Bad deadline."})
        deadline = min(deadline, now + MAX_TTL)
        if deadline <= now:
            return Outcome(
                410,
                {
                    "id": command_id,
                    "error": "expired",
                    "message": "The command is too old to run.",
                },
            )
        try:
            target = music_target(self._store, device_id, params.get("device"))
            action, action_params = _desktop_params(kind, params, target)
        except PolicyError as exc:
            return Outcome(
                403, {"id": command_id, "error": "policy", "message": str(exc)}
            )
        except ValueError as exc:
            return Outcome(
                400, {"id": command_id, "error": "bad_request", "message": str(exc)}
            )

        if kind != "music.status":
            previous = self._store.begin_command(device_id, command_id, kind, now)
            if previous is not None:
                if previous["state"] == "running":
                    return Outcome(
                        409,
                        {
                            "id": command_id,
                            "error": "in_progress",
                            "message": "This command is still running.",
                        },
                    )
                return Outcome(
                    int((previous["result"] or {}).get("status", 200)),
                    (previous["result"] or {}).get("body", {}),
                )

        if kind == "music.status":
            # A read: never queued behind a slow command.
            return await self._call(
                command_id, action, action_params, min(deadline - now, STATUS_TIMEOUT)
            )

        label = LABELS.get(kind, kind).format(
            **{k: params.get(k, "") for k in ("query", "action", "percent")}
        )
        ops = self.operations
        op = ops.start(
            device_id, kind, label, op_id=command_id, deadline=deadline, host="computer"
        )
        task = asyncio.ensure_future(
            self._queued(op["id"], action, action_params, deadline)
        )
        ops.attach(op["id"], task)
        try:
            outcome = await task
        except asyncio.CancelledError:
            outcome = Outcome(
                409,
                {
                    "id": command_id,
                    "error": "cancelled",
                    "message": "Stopping was requested; the computer has not confirmed yet.",
                },
            )
        state = {200: "completed", 410: "expired", 409: "cancelling"}.get(
            outcome.status, "failed"
        )
        if state == "cancelling" and not op.get("action_id"):
            state = "cancelled"  # still queued: nothing reached the computer
        if state != "cancelling":
            ops.finish(op["id"], state, outcome.body)
        self._store.finish_command(
            device_id,
            command_id,
            "done" if outcome.status == 200 else "failed",
            {"status": outcome.status, "body": outcome.body},
            self._clock(),
        )
        return outcome

    async def _queued(
        self, op_id: str, action: str, params: dict[str, Any], deadline: float
    ) -> Outcome:
        ops = self.operations
        ops.update(op_id, state="queued", stage="waiting for the music queue")
        try:
            await asyncio.wait_for(
                self._music.acquire(), max(0.0, deadline - self._clock())
            )
        except asyncio.TimeoutError:
            return Outcome(
                410,
                {
                    "id": op_id,
                    "error": "expired",
                    "message": "Another music command took too long; this one was dropped.",
                },
            )
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return Outcome(
                    410,
                    {
                        "id": op_id,
                        "error": "expired",
                        "message": "The command is too old to run.",
                    },
                )
            ops.update(op_id, state="running", stage="sent to the computer")
            return await self._call(
                op_id,
                action,
                params,
                remaining,
                on_sent=lambda action_id: ops.update(
                    op_id,
                    action_id=action_id,
                    stage="waiting for Spotify on the computer",
                ),
            )
        finally:
            self._music.release()

    async def _call(
        self,
        command_id: str,
        action: str,
        params: dict[str, Any],
        timeout: float,
        *,
        on_sent: Callable[[str], None] | None = None,
    ) -> Outcome:
        try:
            result = await self._hub.call(
                action, params, min(timeout, 40.0), on_sent=on_sent
            )
        except DesktopOfflineError:
            return Outcome(
                503,
                {
                    "id": command_id,
                    "error": "pc_offline",
                    "message": "The computer is not connected.",
                },
            )
        except DesktopTimeoutError:
            return Outcome(
                504,
                {
                    "id": command_id,
                    "error": "timeout",
                    "message": "The computer did not answer in time.",
                },
            )
        if not result.get("ok"):
            return Outcome(
                422,
                {
                    "id": command_id,
                    "error": "action_failed",
                    "message": str(result.get("error") or "The action failed."),
                },
            )
        return Outcome(
            200, {"id": command_id, "ok": True, "result": result.get("result") or {}}
        )
