"""Spotify desktop app control through Spotify's official CLI.

Kept free of LiveKit imports so the Windows helper (jarvis_desktop) can reuse it.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

CLI_TIMEOUT = 30.0
APP_START_TIMEOUT = 20.0
TRANSFER_SETTLE_SECONDS = 1.5
CONFIRM_SECONDS = 4.0  # how long to wait for Spotify to report the new state
PHONE_TYPES = {"smartphone", "phone", "mobile"}
TARGETS = ("computer", "phone")
# Controls that start or keep music going: with a target they first bring playback
# to that device, so "next" never wakes the music up on another one.
PLAYING_CONTROLS = {"resume", "next", "previous"}


def _find_cli() -> str | None:
    found = shutil.which("spotify_cli")
    if found:
        return found
    # Microsoft Store install puts the CLI alias here, which may be missing from PATH.
    alias = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/WindowsApps/spotify_cli.exe"
    )
    return str(alias) if alias.exists() else None


class SpotifyError(Exception):
    pass


class SpotifyCli:
    """Controls the Spotify desktop app on this PC through Spotify's official CLI."""

    def __init__(self) -> None:
        self._cli = _find_cli()

    @property
    def available(self) -> bool:
        return self._cli is not None

    async def _run(self, *args: str) -> str:
        if self._cli is None:
            raise SpotifyError(
                "The Spotify desktop app is not installed on this computer."
            )
        proc = await asyncio.create_subprocess_exec(
            self._cli,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # Stop only this CLI process (never the Spotify app) and reap it, also
            # when the caller cancelled us from outside.
            if proc.returncode is None:
                proc.kill()
            await asyncio.shield(proc.wait())
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SpotifyError("Spotify did not respond in time.") from None
        if proc.returncode != 0:
            message = (stderr or stdout).decode(errors="replace").strip()
            raise SpotifyError(message or f"spotify_cli {args[0]} failed.")
        return stdout.decode(errors="replace")

    async def _json(self, *args: str) -> Any:
        return json.loads(await self._run(*args, "--format", "json"))

    async def status(self) -> dict[str, Any]:
        return await self._json("status")

    async def ensure_running(self) -> None:
        status = await self.status()
        if not status.get("running"):
            await self._run("open")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + APP_START_TIMEOUT
            while not status.get("running"):
                if loop.time() > deadline:
                    raise SpotifyError("The Spotify app did not start.")
                await asyncio.sleep(1.0)
                status = await self.status()
        if not status.get("logged_in"):
            raise SpotifyError("Spotify is open but not logged in.")

    async def devices(self) -> dict[str, Any]:
        return await self._json("devices", "list")

    async def target_device(self, target: str) -> dict[str, Any]:
        """The Spotify Connect device for "computer" (this PC) or "phone"."""
        devices = (await self.devices()).get("devices", [])
        if target == "phone":
            device = next(
                (d for d in devices if d.get("device_type") in PHONE_TYPES), None
            )
            if device is None:
                raise SpotifyError(
                    "The phone is not showing up in Spotify. Open the Spotify app on "
                    "the phone, signed in to the same account, and try again."
                )
            return device
        device = next((d for d in devices if d.get("is_self")), None)
        if device is None:
            raise SpotifyError("Spotify on the computer is not ready yet.")
        return device

    async def search(self, query: str, kind: str) -> dict[str, Any] | None:
        results = await self._json("search", query, "--type", kind, "--limit", "1")
        items = results.get(f"{kind}s") or []
        return items[0] if items else None

    async def play(self, uri: str, device: dict[str, Any]) -> None:
        # `play URI --device X` only transfers playback in this CLI and never starts
        # the requested item, so move playback first and then play on the active one.
        if not device.get("is_active"):
            await self._run("devices", "transfer", device["device_id"])
            await asyncio.sleep(TRANSFER_SETTLE_SECONDS)
        await self._run("play", uri)

    async def play_search(
        self, query: str, kind: str, target: str = "computer"
    ) -> dict[str, Any] | None:
        """Play the best match on the computer or the phone. None when nothing matches."""
        await self.ensure_running()  # the PC app also relays commands to the phone
        device = await self.target_device(target)
        item = await self.search(query, kind)
        if item is None:
            return None
        await self.play(item["uri"], device)
        state = await self.confirm(device, playing=True)
        return {
            "name": item.get("name", ""),
            "artists": item.get("artists") or [],
            "device": device.get("name"),
            "confirmed": state["confirmed"],
        }

    async def is_playing(self) -> bool:
        playing = (await self._json("now-playing")).get("currently_playing") or {}
        return bool(playing.get("is_playing"))

    async def playback_state(self) -> dict[str, Any]:
        """What Spotify reports right now: the track, whether it plays, and where."""
        listing = await self.devices()
        devices = listing.get("devices", [])
        active_id = listing.get("active_device_id")
        active = next(
            (d for d in devices if active_id and d.get("device_id") == active_id),
            next((d for d in devices if d.get("is_active")), None),
        )
        playing = (await self._json("now-playing")).get("currently_playing") or {}
        return {
            "track": playing.get("description"),
            "is_playing": bool(playing.get("is_playing")),
            "device": active.get("name") if active else None,
            "device_id": active.get("device_id") if active else None,
            "on_computer": bool(active and active.get("is_self")),
        }

    async def confirm(self, device: dict[str, Any], *, playing: bool) -> dict[str, Any]:
        """Wait until Spotify reports `device` active and (not) playing.

        A command that returned without error is not proof that the right device
        is playing, so this reads the state back.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_SECONDS
        while True:
            state = await self.playback_state()
            ok = (
                state["device_id"] == device.get("device_id")
                and state["is_playing"] == playing
            )
            if ok or loop.time() >= deadline:
                return {**state, "confirmed": ok}
            await asyncio.sleep(0.5)

    async def bring_to(self, target: str) -> dict[str, Any]:
        """Make the target device the active one, without starting playback."""
        await self.ensure_running()
        device = await self.target_device(target)
        if not device.get("is_active"):
            await self._run("devices", "transfer", device["device_id"])
            await asyncio.sleep(TRANSFER_SETTLE_SECONDS)
        return device

    async def control(self, action: str, target: str | None = None) -> dict[str, Any]:
        """Pause, resume or skip. With a target, resume and skips act only there."""
        device = None
        if target is not None and action in PLAYING_CONTROLS:
            device = await self.bring_to(target)
        try:
            await self._run(action)
        except SpotifyError:
            # The CLI reports an error when pausing something already paused.
            if action == "pause" and not await self.is_playing():
                return {"confirmed": True}
            raise
        if device is None:
            return {}
        return await self.confirm(device, playing=True)

    async def set_volume(
        self, level: float, target: str | None = None
    ) -> dict[str, Any]:
        """Volume applies to the active device, so first make that the target."""
        device = await self.bring_to(target) if target is not None else None
        await self._run("volume", f"{level:.2f}")
        return {"device": device.get("name")} if device else {}
