"""The two things jarvis-control drives: the agent's systemd unit and LiveKit rooms."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from livekit import api
from livekit.protocol.models import ParticipantInfo

logger = logging.getLogger("jarvis_control")

TOKEN_TTL = timedelta(minutes=15)


class UnitError(Exception):
    pass


class AgentUnit:
    """Starts and stops the agent's systemd unit (allowed for us by a polkit rule)."""

    def __init__(self, unit: str, ready_file: Path) -> None:
        self.unit = unit
        self.ready_file = ready_file

    async def _systemctl(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout or stderr).decode(errors="replace").strip()
        return proc.returncode or 0, output

    async def state(self) -> str:
        """active, activating, deactivating, inactive or failed."""
        _, output = await self._systemctl("is-active", self.unit)
        return output.splitlines()[0] if output else "unknown"

    async def start(self) -> None:
        code, output = await self._systemctl("start", "--no-block", self.unit)
        if code != 0:
            raise UnitError(output or f"systemctl start {self.unit} failed")
        logger.info("requested start of %s", self.unit)

    async def stop(self) -> None:
        code, output = await self._systemctl("stop", "--no-block", self.unit)
        if code != 0:
            raise UnitError(output or f"systemctl stop {self.unit} failed")
        logger.info("requested stop of %s", self.unit)

    async def is_ready(self) -> bool:
        """Registered with LiveKit: the agent writes the ready file on registration."""
        return await self.state() == "active" and self.ready_file.exists()


class LiveKitRooms:
    def __init__(self, url: str, key: str, secret: str, agent_name: str) -> None:
        self.url = url
        self._key = key
        self._secret = secret
        self._agent_name = agent_name
        self._api: api.LiveKitAPI | None = None

    def _lk(self) -> api.LiveKitAPI:
        if self._api is None:
            self._api = api.LiveKitAPI(self.url, self._key, self._secret)
        return self._api

    async def aclose(self) -> None:
        if self._api is not None:
            await self._api.aclose()

    async def prepare(self, room: str, metadata: str | None = None) -> None:
        """Create the session's room and dispatch the agent into it exactly once."""
        lk = self._lk()
        await lk.room.create_room(
            api.CreateRoomRequest(
                name=room, empty_timeout=120, departure_timeout=30, max_participants=4
            )
        )
        existing = await lk.agent_dispatch.list_dispatch(room)
        if any(d.agent_name == self._agent_name for d in existing):
            return
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=self._agent_name, room=room, metadata=metadata or ""
            )
        )

    async def users(self, room: str) -> list[str] | None:
        """Identities of non-agent participants, or None when the room is gone."""
        try:
            response = await self._lk().room.list_participants(
                api.ListParticipantsRequest(room=room)
            )
        except api.TwirpError as exc:
            if exc.code == "not_found":
                return None
            raise
        return [
            p.identity
            for p in response.participants
            if p.kind != ParticipantInfo.Kind.AGENT
        ]

    async def delete(self, room: str) -> None:
        try:
            await self._lk().room.delete_room(api.DeleteRoomRequest(room=room))
        except api.TwirpError as exc:
            if exc.code != "not_found":
                raise

    def token(self, room: str, identity: str, name: str) -> str:
        return (
            api.AccessToken(self._key, self._secret)
            .with_identity(identity)
            .with_name(name)
            .with_ttl(TOKEN_TTL)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )
