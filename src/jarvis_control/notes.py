"""Writes files into the JARVIS-Notas folder of the user's Nextcloud, over WebDAV.

The account (NEXTCLOUD_USER, the same one the voice agent uses) can only write
inside that folder, so Nextcloud itself enforces the limit. Here the paths are also
checked: no "..", no absolute paths, only the subfolders JARVIS is meant to use.
Each upload is verified by reading back its size before it counts as saved.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote, urlparse

import aiohttp

ALLOWED_TOP = ("Relatorios", "DiceVTT")


class NotesError(Exception):
    pass


class NotesWriter:
    def __init__(
        self, url: str, user: str, app_password: str, folder: str = "JARVIS-Notas"
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("NEXTCLOUD_URL must be an http(s) URL.")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._root = f"/remote.php/dav/files/{quote(user, safe='')}/"
        self._folder = folder.strip("/")
        self._headers = {"Authorization": aiohttp.encode_basic_auth(user, app_password)}
        self._known_folders: set[str] = set()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> NotesWriter | None:
        url = env.get("NEXTCLOUD_URL", "").strip()
        user = env.get("NEXTCLOUD_USER", "").strip()
        password = env.get("NEXTCLOUD_APP_PASSWORD", "").strip()
        if not (url and user and password):
            return None
        return cls(
            url, user, password, env.get("NEXTCLOUD_NOTES_FOLDER", "JARVIS-Notas")
        )

    def _url(self, relative: str, *, folder: bool = False) -> str:
        parts = [p for p in relative.split("/") if p]
        if (
            not parts
            or parts[0] not in ALLOWED_TOP
            or any(p in {".", ".."} or "\\" in p for p in parts)
        ):
            raise NotesError(f"Not an allowed notes path: {relative}")
        encoded = "/".join(quote(p, safe="") for p in [self._folder, *parts])
        return self._origin + self._root + encoded + ("/" if folder else "")

    async def _ensure_folders(self, http: aiohttp.ClientSession, relative: str) -> None:
        parts = [p for p in relative.split("/") if p][:-1]
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            if folder in self._known_folders:
                continue
            async with http.request("MKCOL", self._url(folder, folder=True)) as resp:
                # 201 created, 405 already there.
                if resp.status not in {201, 405}:
                    raise NotesError(f"Could not create {folder}: HTTP {resp.status}")
            self._known_folders.add(folder)

    async def put(self, relative: str, data: bytes, *, overwrite: bool = False) -> int:
        """Upload and verify; returns the size Nextcloud reports."""
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(
            headers=self._headers, timeout=timeout
        ) as http:
            await self._ensure_folders(http, relative)
            headers = (
                {} if overwrite else {"If-None-Match": "*"}
            )  # never replace a report
            async with http.put(
                self._url(relative), data=data, headers=headers
            ) as resp:
                if resp.status == 412:
                    raise NotesError(f"{relative} already exists")
                if resp.status not in {200, 201, 204}:
                    raise NotesError(f"Upload of {relative} failed: HTTP {resp.status}")
            async with http.request(
                "PROPFIND",
                self._url(relative),
                headers={"Depth": "0"},
                data=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/></d:prop></d:propfind>',
            ) as resp:
                text = await resp.text()
                if resp.status != 207:
                    raise NotesError(f"Could not verify {relative}: HTTP {resp.status}")
        start = text.find("getcontentlength>")
        size = int(text[start + 17 : text.find("<", start + 17)]) if start >= 0 else -1
        if size != len(data):
            raise NotesError(
                f"{relative} was saved with {size} bytes instead of {len(data)}"
            )
        return size
