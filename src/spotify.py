from typing import Any, Literal, Protocol

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

if __package__:
    from .desktop_link import DesktopLink, DesktopLinkError
    from .spotify_cli import SpotifyCli, SpotifyError
else:
    from desktop_link import DesktopLink, DesktopLinkError
    from spotify_cli import SpotifyCli, SpotifyError


class SpotifyBackend(Protocol):
    @property
    def available(self) -> bool: ...

    async def play_search(
        self, query: str, kind: str, target: str = "computer"
    ) -> dict[str, Any] | None: ...

    async def control(self, action: str, target: str | None = None) -> Any: ...

    async def set_volume(self, level: float, target: str | None = None) -> Any: ...


class RemoteSpotify:
    """Spotify on the user's paired Windows PC, reached through jarvis-control."""

    def __init__(self, link: DesktopLink) -> None:
        self._link = link

    @property
    def available(self) -> bool:
        return True

    async def _call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._link.call(action, params)
        except DesktopLinkError as exc:
            raise SpotifyError(str(exc)) from exc

    async def play_search(
        self, query: str, kind: str, target: str = "computer"
    ) -> dict[str, Any] | None:
        result = await self._call(
            "spotify.play", {"query": query, "kind": kind, "device": target}
        )
        return result.get("item")

    async def control(self, action: str, target: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"action": action}
        if target is not None:
            params["device"] = target
        return await self._call("spotify.control", params)

    async def set_volume(
        self, level: float, target: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"level": level}
        if target is not None:
            params["device"] = target
        return await self._call("spotify.volume", params)


class SimulatedSpotify:
    """In-memory Spotify for simulations: records what the agent asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    @property
    def available(self) -> bool:
        return True

    async def play_search(
        self, query: str, kind: str, target: str = "computer"
    ) -> dict[str, Any] | None:
        self.calls.append(("play", query, kind, target))
        device = "BATCOMPUTADOR" if target == "computer" else "Moto g8 power lite"
        return {
            "name": query.title(),
            "artists": [],
            "device": device,
            "confirmed": True,
        }

    async def control(self, action: str, target: str | None = None) -> dict[str, Any]:
        self.calls.append(("control", action, target))
        return {"confirmed": True}

    async def set_volume(
        self, level: float, target: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("volume", level, target))
        return {}


class SpotifyTools:
    def __init__(
        self, spotify: SpotifyBackend | None = None, *, locked_target: str | None = None
    ) -> None:
        self.spotify: SpotifyBackend = spotify or SpotifyCli()
        # The master phone: music only on the computer, whatever is asked.
        self.locked_target = locked_target

    @property
    def available(self) -> bool:
        """Whether a Spotify app can be controlled, locally or on the paired PC.

        Without one the tools are left out instead of failing on every request.
        """
        return self.spotify.available

    @property
    def tools(self) -> list:
        if not self.available:
            return []
        play = (
            self.play_on_computer
            if self.locked_target == "computer"
            else self.play_on_spotify
        )
        return [play, self.control_spotify, self.set_spotify_volume]

    async def _play(self, query: str, kind: str, device: str) -> str:
        query = query.strip()
        if not query:
            raise ToolError("Tell me what to play.")
        try:
            item = await self.spotify.play_search(query, kind, device)
        except SpotifyError as exc:
            raise ToolError(str(exc)) from exc
        if item is None:
            raise ToolError(f"Nothing found on Spotify for {query}.")

        artists = ", ".join(item.get("artists") or [])
        where = f" on {item['device']}" if item.get("device") else ""
        reply = (
            f"Now playing {item['name']}"
            + (f" by {artists}" if artists else "")
            + where
        )
        if item.get("confirmed") is False:
            reply += ", although Spotify has not confirmed that it is playing yet"
        return reply

    @function_tool(name="play_on_spotify")
    async def play_on_computer(
        self,
        context: RunContext,
        query: str,
        kind: Literal["track", "artist", "album", "playlist"] = "track",
    ) -> str:
        """Play music on Spotify on the user's computer, the BATCOMPUTADOR.

        Use this whenever the user asks to play a song, artist, album, or playlist.
        Music always plays on the computer, never on the phone or in the browser.

        Args:
            query: What to search for, e.g. the song title followed by the artist name.
            kind: "track" for a specific song, "artist" to play an artist,
                "album" for an album, or "playlist" for a playlist or mood.
        """
        return await self._play(query, kind, "computer")

    @function_tool()
    async def play_on_spotify(
        self,
        context: RunContext,
        query: str,
        device: Literal["computer", "phone"],
        kind: Literal["track", "artist", "album", "playlist"] = "track",
    ) -> str:
        """Play music on Spotify, on the user's computer or on their phone.

        Use this whenever the user asks to play a song, artist, album, or playlist.
        Music is always played through this tool, never in the browser.

        Args:
            query: What to search for, e.g. the song title followed by the artist name.
            device: "computer" or "phone", exactly as the user said. Ask the user
                first if they did not say where.
            kind: "track" for a specific song, "artist" to play an artist,
                "album" for an album, or "playlist" for a playlist or mood.
        """
        return await self._play(query, kind, device)

    @function_tool()
    async def control_spotify(
        self,
        context: RunContext,
        action: Literal["pause", "resume", "next", "previous"],
    ) -> str:
        """Pause, resume, or skip the music playing in Spotify on the user's computer.

        Args:
            action: "pause", "resume", "next" to skip forward, or "previous" to go back.
        """
        try:
            result = await self.spotify.control(action, self.locked_target)
        except SpotifyError as exc:
            raise ToolError(str(exc)) from exc
        if isinstance(result, dict) and result.get("confirmed") is False:
            return f"Spotify: {action} sent, but the computer has not confirmed it yet."
        return f"Spotify: {action} done."

    @function_tool()
    async def set_spotify_volume(self, context: RunContext, percent: int) -> str:
        """Set the Spotify volume on the user's computer.

        Args:
            percent: Volume from 0 to 100.
        """
        percent = max(0, min(100, percent))
        try:
            await self.spotify.set_volume(percent / 100, self.locked_target)
        except SpotifyError as exc:
            raise ToolError(str(exc)) from exc
        return f"Spotify volume set to {percent} percent."
