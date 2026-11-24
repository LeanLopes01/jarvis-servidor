"""Same-origin browser shell; tokens remain in Secure/HttpOnly cookies."""

import json
import mimetypes
import os
import socket
import time
from pathlib import Path

from aiohttp import web

from .store import token_hash

COOKIE = "__Host-jarvis"


def browser_surface(store, limiter):
    root = Path(__file__).resolve().parents[2] / "deploy/web"
    origin = os.getenv(
        "JARVIS_WEB_ORIGIN", "https://your-jarvis-api.example.com"
    ).rstrip("/")
    store._db.execute(
        "CREATE TABLE IF NOT EXISTS browser_sessions(hash TEXT PRIMARY KEY, expires REAL NOT NULL)"
    )

    def valid(request):
        token = request.cookies.get(COOKIE, "")
        row = store._db.execute(
            "SELECT expires FROM browser_sessions WHERE hash=?", (token_hash(token),)
        ).fetchone()
        return bool(
            row and row[0] > time.time() and store.device_for_token(token, time.time())
        )

    def same_origin(request):
        return (
            request.headers.get("Origin") == origin
            and request.headers.get("X-Orchestrator-Request") == "1"
        )

    @web.middleware
    async def middleware(request, handler):
        cookie = request.cookies.get(COOKIE)
        if (
            cookie
            and not request.headers.get("Authorization")
            and request.path
            not in {
                "/api/browser-session",
                "/api/browser-pair",
                "/api/browser-logout",
                "/",
            }
            and not request.path.startswith("/assets/")
        ):
            if not valid(request):
                return web.json_response(
                    {"message": "A sessão expirou. Pareie este navegador novamente."},
                    status=401,
                )
            if request.method not in {"GET", "HEAD"} and not same_origin(request):
                return web.json_response({"message": "Origem inválida."}, status=403)
        if request.path.startswith("/api/") and request.path not in {
            "/api/browser-session",
            "/api/browser-pair",
            "/api/browser-logout",
        }:
            if not valid(request):
                return web.json_response(
                    {"message": "Pareie este navegador."}, status=401
                )
            path = None
            if request.path.startswith("/api/orchestrator/v1/"):
                path = request.path.removeprefix("/api/orchestrator")
            elif request.path.startswith("/api/rag/"):
                path = request.path.replace("/api/rag/", "/v1/rag/", 1)
            if path:
                cloned = request.clone(
                    rel_url=path
                    + ("?" + request.query_string if request.query_string else "")
                )
                match = await request.app.router.resolve(cloned)
                cloned._match_info = match
                try:
                    response = await match.handler(cloned)
                except web.HTTPException as exc:
                    response = web.json_response(
                        {"message": exc.reason}, status=exc.status
                    )
            elif request.path == "/api/catalog":
                catalog = root / "catalog.json"
                response = web.json_response(
                    json.loads(catalog.read_text(encoding="utf-8"))
                )
            elif request.path == "/api/system/status":
                memory = {
                    line.split(":")[0]: int(line.split()[1]) * 1024
                    for line in Path("/proc/meminfo").read_text().splitlines()
                }
                response = web.json_response(
                    {
                        "scope": "server",
                        "host": socket.gethostname(),
                        "cpu_count": os.cpu_count(),
                        "memory_total": memory["MemTotal"],
                        "memory_free": memory["MemAvailable"],
                        "uptime_seconds": float(
                            Path("/proc/uptime").read_text().split()[0]
                        ),
                        "runtime": "production",
                    }
                )
            else:
                response = web.json_response(
                    {
                        "message": "Este recurso requer o aplicativo no PC. O RAG remoto está na tela RAG local."
                    },
                    status=503,
                )
        else:
            response = await handler(request)
        if (
            request.path.startswith("/api/")
            or request.path == "/"
            or request.path.startswith("/assets/")
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    async def session(request):
        token = request.cookies.get(COOKIE, "")
        device = store.device_for_token(token, time.time()) if valid(request) else None
        return web.json_response(
            {
                "remote": True,
                "authenticated": bool(device),
                "device_id": device.id if device else None,
            }
        )

    async def pair(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        if not limiter.hit("browser-pair:" + str(request.remote), 10, 900):
            raise web.HTTPTooManyRequests()
        body = await request.json()
        redeemed = store.redeem_pairing_code(
            str(body.get("code", "")),
            "Navegador · " + str(body.get("name", "Jarvis"))[:40],
            time.time(),
        )
        if not redeemed:
            return web.json_response(
                {"message": "Código inválido ou expirado."}, status=400
            )
        device, token = redeemed
        store._db.execute(
            "INSERT INTO browser_sessions VALUES(?,?)",
            (token_hash(token), time.time() + 604800),
        )
        response = web.json_response({"device_id": device.id, "authenticated": True})
        response.set_cookie(
            COOKIE,
            token,
            max_age=604800,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def logout(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        store._db.execute(
            "DELETE FROM browser_sessions WHERE hash=?",
            (token_hash(request.cookies.get(COOKIE, "")),),
        )
        response = web.json_response({"ok": True})
        response.del_cookie(
            COOKIE, path="/", secure=True, httponly=True, samesite="Strict"
        )
        return response

    async def static(request):
        name = request.match_info.get("asset", "index.html")
        path = (
            root / ("assets" if "asset" in request.match_info else "") / name
        ).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(
            body=path.read_bytes(),
            content_type=mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
        )

    return middleware, [
        web.get("/api/browser-session", session),
        web.post("/api/browser-pair", pair),
        web.post("/api/browser-logout", logout),
        web.get("/", static),
        web.get("/assets/{asset:.+}", static),
    ]
