"""Session lifecycle: start the agent for a session, watch the room, stop when idle.

States: starting -> ready (room created, agent dispatched, token available)
-> active (a user is in the room) -> ended/failed. A supervisor tick every few
seconds moves sessions along and decides when the agent unit may stop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .store import Session, Store

logger = logging.getLogger("jarvis_control")


class BusyError(Exception):
    """Another device has a live session (one session at a time for now)."""


class NotFoundError(Exception):
    pass


class Unit(Protocol):
    async def state(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def is_ready(self) -> bool: ...


class Rooms(Protocol):
    async def prepare(self, room: str, metadata: str | None = None) -> None: ...

    async def users(self, room: str) -> list[str] | None: ...

    async def delete(self, room: str) -> None: ...


@dataclass(frozen=True)
class Timing:
    start_timeout: float = 60.0  # agent registered and dispatched
    join_timeout: float = 90.0  # token issued, user never joined
    reconnect_grace: float = 90.0  # user gone from an active room
    idle_stop_grace: float = 30.0  # no live sessions before stopping the agent
    max_duration: float = 6 * 3600.0
    livekit_outage_limit: float = 600.0  # room checks failing and no heartbeat
    heartbeat_fresh: float = 60.0
    idempotency_window: float = 600.0
    start_retries: int = 1


class SessionManager:
    def __init__(
        self,
        store: Store,
        unit: Unit,
        rooms: Rooms,
        *,
        timing: Timing | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._unit = unit
        self._rooms = rooms
        self._timing = timing or Timing()
        self._clock = clock
        self._lock = asyncio.Lock()
        self._idle_since = clock()
        self._hold_until = 0.0  # a routine is about to start: keep the agent up
        self._livekit_failing_since: dict[str, float] = {}
        self._start_requests: dict[str, int] = {}

    async def prewarm(self, until: float) -> None:
        """Start the agent ahead of a routine and keep it up until `until`."""
        async with self._lock:
            self._hold_until = max(self._hold_until, until)
            if await self._unit.state() not in {"active", "activating"}:
                await self._unit.start()

    def get(self, device_id: str, session_id: str) -> Session:
        session = self._store.session(session_id)
        if session is None or session.device_id != device_id:
            raise NotFoundError
        return session

    async def create(
        self,
        device_id: str,
        idempotency_key: str | None,
        context: dict[str, Any] | None = None,
    ) -> Session:
        async with self._lock:
            now = self._clock()
            if idempotency_key:
                existing = self._store.session_for_key(
                    device_id, idempotency_key, now - self._timing.idempotency_window
                )
                if existing is not None:
                    return existing
            for live in self._store.live_sessions():
                if live.device_id != device_id:
                    raise BusyError
                # The same phone starting again (app restarted): replace its session.
                await self._finish(live, now, "replaced")
            session = self._store.create_session(
                device_id, idempotency_key, now, context
            )
            self._start_requests[session.id] = 1
            logger.info("session %s starting", session.id[:8])
            try:
                await self._unit.start()
            except Exception:
                await self._finish(session, now, "agent_start_failed", state="failed")
                raise
            return self._store.session(session.id) or session

    async def end(self, device_id: str, session_id: str, reason: str) -> None:
        async with self._lock:
            session = self.get(device_id, session_id)
            if session.live:
                await self._finish(session, self._clock(), reason)

    def heartbeat(self, device_id: str, session_id: str) -> Session:
        session = self.get(device_id, session_id)
        if session.live:
            now = self._clock()
            self._store.update_session(session.id, now, last_heartbeat_at=now)
        return self.get(device_id, session_id)

    async def _finish(
        self, session: Session, now: float, reason: str, *, state: str = "ended"
    ) -> None:
        self._store.update_session(
            session.id, now, state=state, ended_at=now, end_reason=reason
        )
        self._livekit_failing_since.pop(session.id, None)
        self._start_requests.pop(session.id, None)
        self._idle_since = now  # the stop grace counts from the end of the last session
        logger.info("session %s %s: %s", session.id[:8], state, reason)
        try:
            await self._rooms.delete(session.room)
        except Exception:
            logger.warning("could not delete room %s", session.room, exc_info=True)

    async def tick(self) -> None:
        async with self._lock:
            now = self._clock()
            unit_state = await self._unit.state()
            for session in self._store.live_sessions():
                try:
                    await self._supervise(session, now, unit_state)
                except Exception:
                    logger.exception("error supervising session %s", session.id[:8])

            if self._store.live_sessions():
                self._idle_since = now
            elif (
                unit_state in {"active", "activating"}
                and now - self._idle_since >= self._timing.idle_stop_grace
                and now >= self._hold_until
            ):
                await self._unit.stop()

    async def _supervise(self, session: Session, now: float, unit_state: str) -> None:
        timing = self._timing
        age = now - session.created_at
        if age > timing.max_duration:
            await self._finish(session, now, "max_duration")
            return

        if session.state == "starting":
            if unit_state == "active" and await self._unit.is_ready():
                try:
                    await self._rooms.prepare(session.room, session.context)
                except Exception:
                    logger.warning(
                        "dispatch failed for %s", session.id[:8], exc_info=True
                    )
                    if age > timing.start_timeout:
                        await self._finish(
                            session, now, "dispatch_failed", state="failed"
                        )
                    return
                self._store.update_session(session.id, now, state="ready", ready_at=now)
                logger.info("session %s ready after %.1fs", session.id[:8], age)
            elif age > timing.start_timeout:
                await self._finish(session, now, "agent_start_timeout", state="failed")
            elif unit_state in {"inactive", "failed"} and age > 5:
                # The agent exited before registering; try again once.
                attempts = self._start_requests.get(session.id, 1)
                if attempts <= timing.start_retries:
                    self._start_requests[session.id] = attempts + 1
                    logger.warning(
                        "agent not running; restarting for %s", session.id[:8]
                    )
                    await self._unit.start()
            return

        if unit_state != "active":
            await self._finish(session, now, "agent_stopped", state="failed")
            return

        try:
            users = await self._rooms.users(session.room)
        except Exception:
            since = self._livekit_failing_since.setdefault(session.id, now)
            heartbeat_fresh = (
                session.last_heartbeat_at is not None
                and now - session.last_heartbeat_at < timing.heartbeat_fresh
            )
            logger.warning("room check failed for %s", session.id[:8], exc_info=True)
            if now - since > timing.livekit_outage_limit and not heartbeat_fresh:
                await self._finish(session, now, "livekit_unreachable")
            return
        self._livekit_failing_since.pop(session.id, None)

        if users is None:
            await self._finish(session, now, "room_closed")
        elif users:
            self._store.update_session(
                session.id, now, state="active", last_presence_at=now
            )
        elif session.state == "ready":
            if now - (session.ready_at or session.created_at) > timing.join_timeout:
                await self._finish(session, now, "client_never_joined")
        elif now - (session.last_presence_at or now) > timing.reconnect_grace:
            await self._finish(session, now, "client_left")
