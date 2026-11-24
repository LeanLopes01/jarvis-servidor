"""Authenticated Android primary channel; companion is deliberately unpublished."""

import json
from pathlib import Path

from aiohttp import web


def routes(require):
    folder = Path(__file__).resolve().parents[2] / "deploy/mobile"

    @require("work", work=True)
    async def updates(request):
        manifest = folder / "primary.json"
        if (
            request.query.get("channel", "primary") != "primary"
            or not manifest.is_file()
        ):
            return web.json_response(
                {
                    "available": False,
                    "message": "Nenhuma versão publicada para este canal.",
                }
            )
        return web.json_response(
            {"available": True, **json.loads(manifest.read_text(encoding="utf-8"))}
        )

    @require("app", work=True)
    async def package(request):
        path = folder / "jarvis-primary.apk"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Type": "application/vnd.android.package-archive",
            },
        )

    return [web.get("/v1/updates", updates), web.get("/v1/updates/package", package)]
