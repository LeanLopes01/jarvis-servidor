"""Explicit, one-shot execution of the pinned n8n catalog. No provider secrets here."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web


class Catalog:
    def __init__(self, store, registry=None, base_url=None, secret=None):
        self.store = store
        self.db = store._db
        path = Path(
            registry
            or Path(__file__).resolve().parents[2] / "deploy/catalog/registry.json"
        )
        self.entries = (
            {
                e["id"]: e
                for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
            }
            if path.exists()
            else {}
        )
        self.base_url = (base_url or os.getenv("JARVIS_PERSONAL_N8N_URL", "")).rstrip(
            "/"
        )
        self.secret = secret or os.getenv("JARVIS_PERSONAL_N8N_SECRET", "")
        # The URL used by personal.py includes /webhook; accept that exact prefix.
        if self.base_url.endswith("/webhook"):
            self.base_url = self.base_url[:-8]
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS catalog_connections(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS catalog_calls(
              id TEXT PRIMARY KEY, device_id TEXT NOT NULL, connection_id TEXT NOT NULL,
              tool TEXT NOT NULL, arguments TEXT NOT NULL, digest TEXT NOT NULL,
              status TEXT NOT NULL, expires REAL NOT NULL, result TEXT);
        """)
        # A process restart must never retry a call that may have reached the provider.
        self.db.execute(
            "UPDATE catalog_calls SET status='unknown' WHERE status='running'"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS catalog_one_running ON catalog_calls(connection_id) WHERE status='running'"
        )
        self.db.commit()

    def enabled(self, slug):
        row = self.db.execute(
            "SELECT enabled FROM catalog_connections WHERE id=?", (slug,)
        ).fetchone()
        return bool(row and row[0])

    def view(self):
        return {
            "entries": [
                {**e, "enabled": self.enabled(e["id"])} for e in self.entries.values()
            ],
            "configured": bool(self.base_url and self.secret),
        }

    def toggle(self, slug, enabled):
        if slug not in self.entries or not isinstance(enabled, bool):
            raise web.HTTPBadRequest(reason="Conexão inválida.")
        self.db.execute(
            "INSERT INTO catalog_connections VALUES(?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled",
            (slug, int(enabled)),
        )
        self.db.commit()
        return {"id": slug, "enabled": enabled}

    def prepare(self, device, slug, body):
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="Envie um objeto JSON.")
        if slug not in self.entries or not self.enabled(slug):
            raise web.HTTPForbidden(
                reason="Habilite a conexão depois de configurá-la no n8n."
            )
        tool, args = body.get("tool"), body.get("arguments")
        if tool not in self.entries[slug]["tools"] or not isinstance(args, dict):
            raise web.HTTPBadRequest(reason="Ferramenta ou argumentos inválidos.")
        raw = json.dumps(
            args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(raw.encode()) > 32000:
            raise web.HTTPRequestEntityTooLarge(
                max_size=32000, actual_size=len(raw.encode())
            )
        now, ident = time.time(), uuid.uuid4().hex
        digest = hashlib.sha256((slug + "\n" + tool + "\n" + raw).encode()).hexdigest()
        self.db.execute(
            "DELETE FROM catalog_calls WHERE expires < ? AND status=?",
            (now - 86400, "pending"),
        )
        self.db.execute(
            "INSERT INTO catalog_calls VALUES(?,?,?,?,?,?,?,?,NULL)",
            (ident, device, slug, tool, raw, digest, "pending", now + 300),
        )
        self.db.commit()
        return {
            "id": ident,
            "connection": slug,
            "tool": tool,
            "arguments": args,
            "digest": digest,
            "expires_at": now + 300,
            "message": "Revise os argumentos. A execução pode enviar dados ou alterar a conta.",
        }

    def result(self, device, ident):
        row = self.db.execute(
            "SELECT status,result FROM catalog_calls WHERE id=? AND device_id=?",
            (ident, device),
        ).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        return {
            "id": ident,
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    async def execute(self, device, ident, digest):
        row = self.db.execute(
            "SELECT * FROM catalog_calls WHERE id=? AND device_id=?", (ident, device)
        ).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        if (
            row["digest"] != digest
            or row["expires"] < time.time()
            or not self.enabled(row["connection_id"])
        ):
            raise web.HTTPConflict(
                reason="Plano expirado, alterado ou conexão pausada. Prepare novamente."
            )
        if self.base_url != "http://127.0.0.1:5679" or not self.secret:
            raise web.HTTPServiceUnavailable(
                reason="Gateway do catálogo não configurado no servidor."
            )
        try:
            changed = self.db.execute(
                "UPDATE catalog_calls SET status='running' WHERE id=? AND status='pending'",
                (ident,),
            ).rowcount
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(
                reason="Já existe uma chamada em andamento nesta conexão."
            ) from None
        self.db.commit()
        if not changed:
            return self.result(device, ident)
        status, result = (
            "unknown",
            {
                "message": "Não foi possível confirmar a execução. Confira a conta antes de repetir."
            },
        )
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=75)
                ) as session,
                session.post(
                    self.base_url + "/webhook/jarvis-catalog/" + row["connection_id"],
                    headers={"X-Jarvis-Gateway": self.secret},
                    json={
                        "tool": row["tool"],
                        "arguments": json.loads(row["arguments"]),
                    },
                    allow_redirects=False,
                ) as response,
            ):
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > 150000:
                        break
                if response.status == 200 and len(data) <= 150000:
                    result = json.loads(data)
                    errors = result if isinstance(result, list) else [result]
                    status = (
                        "failed"
                        if any(
                            isinstance(item, dict)
                            and (item.get("error") or item.get("isError"))
                            for item in errors
                        )
                        else "completed"
                    )
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
        self.db.execute(
            "UPDATE catalog_calls SET status=?,result=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), ident),
        )
        self.db.commit()
        return self.result(device, ident)


def routes(catalog, require, device_key):
    async def body(request):
        try:
            value = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(reason="JSON inválido.") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(reason="Envie um objeto JSON.")
        return value

    @require("work", personal=True)
    async def listing(request):
        return web.json_response(catalog.view())

    @require("work", personal=True)
    async def toggle(request):
        data = await body(request)
        return web.json_response(
            catalog.toggle(request.match_info["slug"], data.get("enabled"))
        )

    @require("work", personal=True)
    async def prepare(request):
        return web.json_response(
            catalog.prepare(
                request[device_key].id, request.match_info["slug"], await body(request)
            ),
            status=201,
        )

    @require("work", personal=True)
    async def execute(request):
        data = await body(request)
        return web.json_response(
            await catalog.execute(
                request[device_key].id, request.match_info["ident"], data.get("digest")
            )
        )

    @require("work", personal=True)
    async def result(request):
        return web.json_response(
            catalog.result(request[device_key].id, request.match_info["ident"])
        )

    return [
        web.get("/v1/extensions", listing),
        web.patch("/v1/extensions/{slug}", toggle),
        web.post("/v1/extensions/{slug}/prepare", prepare),
        web.post("/v1/extension-calls/{ident}/execute", execute),
        web.get("/v1/extension-calls/{ident}", result),
    ]
