"""Opens pages and Google searches where the user can see them: in Firefox on the
PC (the default) or in the phone's browser through the Flutter app."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Literal

from livekit import rtc
from livekit.agents import RunContext, function_tool, get_job_context
from livekit.agents.llm import ToolError

if __package__:
    from .desktop_link import DesktopLink, DesktopLinkError
    from .jarvis_desktop.browser_open import BrowserOpenError, open_on_pc, target_url
    from .phone import _phone_identity
else:
    from desktop_link import DesktopLink, DesktopLinkError
    from jarvis_desktop.browser_open import BrowserOpenError, open_on_pc, target_url
    from phone import _phone_identity

PHONE_OPEN_METHOD = "browser.open"


class DeviceBrowserTools:
    def __init__(
        self, link: DesktopLink | None, pc_available: bool, *, simulation: bool = False
    ) -> None:
        self._link = link
        self._simulation = simulation
        # The agent itself runs on the PC (development): open Firefox directly.
        self._local = sys.platform == "win32"
        self._pc_available = pc_available or self._local

    @property
    def tools(self) -> list:
        return [self.open_on_device]

    async def _open_on_computer(self, what: str) -> str:
        if self._local:
            try:
                result = await asyncio.to_thread(open_on_pc, what)
            except BrowserOpenError as exc:
                raise ToolError(str(exc)) from exc
            return f"Opened {result['url']} in {result['browser']} on the computer."
        if self._link is None or not self._pc_available:
            raise ToolError("The user's computer is not connected right now.")
        try:
            result = await self._link.call("browser.open", {"what": what})
        except DesktopLinkError as exc:
            raise ToolError(str(exc)) from exc
        return f"Opened {result.get('url')} in {result.get('browser')} on the computer."

    async def _open_on_phone(self, url: str) -> str:
        room = get_job_context().room
        try:
            response = await room.local_participant.perform_rpc(
                destination_identity=_phone_identity(room),
                method=PHONE_OPEN_METHOD,
                payload=json.dumps({"url": url}),
                response_timeout=15.0,
            )
        except rtc.RpcError as exc:
            raise ToolError(f"The phone could not open it: {exc.message}") from exc
        if response != "opened":
            raise ToolError("The phone could not open it. Is the JARVIS app in front?")
        return f"Opened {url} on the phone."

    @function_tool()
    async def open_on_device(
        self,
        context: RunContext,
        what: str,
        device: Literal["computer", "phone"] = "computer",
    ) -> str:
        """Open a website, or a Google search, where the user can see it.

        Use ONLY when the user explicitly asks to OPEN or SHOW a page on a device.
        Ordinary searches and 'research and tell me' use search_the_web and a
        spoken answer, NEVER this tool. The computer is the default only for an
        explicit request to open a page; use the phone when specified.

        Args:
            what: A web address (e.g. "youtube.com" or a full https link), or the
                words to search on Google.
            device: "computer" (default) or "phone".
        """
        try:
            url = target_url(what)
        except BrowserOpenError as exc:
            raise ToolError(str(exc)) from exc
        if self._simulation:
            return f"Simulation: opened {url} on {device}."
        if device == "phone":
            return await self._open_on_phone(url)
        return await self._open_on_computer(what)
