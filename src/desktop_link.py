"""The agent's side of the link to the user's Windows PC.

The PC runs jarvis_desktop, which keeps an outbound WebSocket open to jarvis-control.
The agent never talks to the PC directly: it asks jarvis-control's local-only internal
API to run a named action there and waits for the result.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp

DEFAULT_TIMEOUT = 25.0


class DesktopLinkError(Exception):
    """A user-facing failure to run something on the user's computer."""


class DesktopLink:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_env(cls) -> DesktopLink | None:
        url = os.environ.get("JARVIS_CONTROL_INTERNAL_URL", "").strip()
        token = os.environ.get("JARVIS_INTERNAL_TOKEN", "").strip()
        return cls(url, token) if url and token else None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def status(self) -> dict[str, Any]:
        """{"online": bool, "device": str | None, "capabilities": [...]}."""
        try:
            async with self._client().get(
                f"{self._base_url}/internal/v1/desktop",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DesktopLinkError(
                "The home server's control service is unreachable."
            ) from exc

    async def call(
        self, action: str, params: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        try:
            async with self._client().post(
                f"{self._base_url}/internal/v1/desktop/actions",
                json={"action": action, "params": params, "timeout": timeout},
                timeout=aiohttp.ClientTimeout(total=timeout + 5),
            ) as resp:
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise DesktopLinkError(
                "I could not reach the user's computer through the home server."
            ) from exc

        if resp.status == 200 and body.get("ok"):
            return body.get("result") or {}
        error = body.get("error")
        if error == "pc_offline":
            raise DesktopLinkError("The user's computer is not connected right now.")
        if error == "timeout":
            raise DesktopLinkError("The user's computer did not answer in time.")
        raise DesktopLinkError(
            body.get("message") or "The action failed on the computer."
        )
