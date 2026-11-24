"""Routines: scheduled moments when Jarvis speaks first, like the morning briefing.

The phone schedules an Android alarm for each routine and opens a session with the
routine's id when it fires; the server only keeps the routines and starts the agent
a little early, so it is already registered when the alarm goes off.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

logger = logging.getLogger("jarvis_control")

# Brazil has not used daylight saving time since 2019, so GMT-3 is fixed.
TIMEZONE = timezone(timedelta(hours=-3), "GMT-3")
PREWARM_LEAD = 45.0  # seconds before the alarm
PREWARM_HOLD = 300.0  # keep the agent up this long after the alarm time

DEFAULT_ROUTINES = [
    {
        "id": "wake-up",
        "name": "Wake up",
        "enabled": True,
        "time": "06:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "location": "Lorena, São Paulo, Brazil",
        "prompt": (
            "Morning briefing. Wish the user a good morning, tell them the current "
            "temperature and today's forecast for their location, and share one "
            "interesting news story picked at random from today's headlines."
        ),
    }
]


class RoutineError(ValueError):
    pass


def validate(body: Any, routine_id: str | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RoutineError("Send the routine as a JSON object.")
    name = str(body.get("name", "")).strip()[:40]
    when = str(body.get("time", "")).strip()
    prompt = str(body.get("prompt", "")).strip()[:600]
    location = (
        str(body.get("location", "")).strip()[:80] or DEFAULT_ROUTINES[0]["location"]
    )
    days = body.get("days", [])
    if not name:
        raise RoutineError("The routine needs a name.")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", when):
        raise RoutineError("Time must be HH:MM, from 00:00 to 23:59.")
    if (
        not isinstance(days, list)
        or not days
        or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)
    ):
        raise RoutineError("Days must be a list of weekdays from 0 (Monday) to 6.")
    if not prompt:
        raise RoutineError("Tell Jarvis what to do in this routine.")
    return {
        "id": routine_id or body.get("id"),
        "name": name,
        "enabled": bool(body.get("enabled", True)),
        "time": when,
        "days": sorted(set(days)),
        "prompt": prompt,
        "location": location,
    }


def next_run(routine: dict[str, Any], now: float) -> float | None:
    """The next time (epoch seconds) the routine fires strictly after `now`."""
    if not routine.get("enabled") or not routine.get("days"):
        return None
    hour, minute = (int(part) for part in routine["time"].split(":"))
    local = datetime.fromtimestamp(now, TIMEZONE)
    for offset in range(8):
        day = local + timedelta(days=offset)
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.weekday() in routine["days"] and candidate.timestamp() > now:
            return candidate.timestamp()
    return None


class Prewarmer(Protocol):
    async def prewarm(self, until: float) -> None: ...


class RoutineScheduler:
    """Starts the agent shortly before each routine fires."""

    def __init__(
        self,
        routines: Callable[[], list[dict[str, Any]]],
        manager: Prewarmer,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._routines = routines
        self._manager = manager
        self._clock = clock
        self._done: set[tuple[str, float]] = set()

    async def tick(self) -> None:
        now = self._clock()
        for routine in self._routines():
            run = next_run(routine, now)
            if run is None or run - now > PREWARM_LEAD:
                continue
            key = (routine["id"], run)
            if key in self._done:
                continue
            self._done.add(key)
            logger.info("prewarming the agent for routine %s", routine["name"])
            await self._manager.prewarm(until=run + PREWARM_HOLD)
        self._done = {key for key in self._done if key[1] > now - 3600}
