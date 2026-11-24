import json
import time

import pytest
from aiohttp import web

from src.jarvis_control.browser import COOKIE
from src.jarvis_control.extensions import Catalog
from src.jarvis_control.store import Store
from tests.test_control import api, parts  # noqa: F401


def catalog(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"entries": [{"id": "demo", "tools": ["create"], "name": "Demo"}]})
    )
    return Catalog(
        Store(":memory:"),
        registry=path,
        base_url="http://127.0.0.1:5679",
        secret="example",
    )


async def test_catalog_disabled_expired_and_one_shot(tmp_path):
    c = catalog(tmp_path)
    with pytest.raises(web.HTTPForbidden):
        c.prepare("a", "demo", {"tool": "create", "arguments": {}})
    c.toggle("demo", True)
    p = c.prepare("a", "demo", {"tool": "create", "arguments": {"title": "hello"}})
    with pytest.raises(web.HTTPNotFound):
        await c.execute("other-device", p["id"], p["digest"])
    with pytest.raises(web.HTTPConflict):
        await c.execute("a", p["id"], "tampered")
    c.db.execute("UPDATE catalog_calls SET status='running' WHERE id=?", (p["id"],))
    assert (await c.execute("a", p["id"], p["digest"]))["status"] == "running"
    c.db.execute("UPDATE catalog_calls SET status='unknown' WHERE id=?", (p["id"],))
    assert (await c.execute("a", p["id"], p["digest"]))["status"] == "unknown"
    c.toggle("demo", False)
    with pytest.raises(web.HTTPConflict):
        await c.execute("a", p["id"], p["digest"])


async def test_browser_cookie_csrf_expiry_and_permissions(api):  # noqa: F811
    client, store, _ = api
    origin = "https://your-jarvis-api.example.com"
    code = store.create_pairing_code("app", 600, time.time())
    assert (await client.post("/api/browser-pair", json={"code": code})).status == 403
    response = await client.post(
        "/api/browser-pair",
        json={"code": code},
        headers={"Origin": origin, "X-Orchestrator-Request": "1"},
    )
    assert response.status == 200
    body = await response.json()
    assert "device_token" not in body
    cookie = response.cookies[COOKIE]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    headers = {"Cookie": f"{COOKIE}={cookie.value}"}
    assert (
        await client.get("/api/orchestrator/v1/integrations", headers=headers)
    ).status == 403
    store.set_personal_access(body["device_id"], True)
    assert (
        await client.get("/api/orchestrator/v1/integrations", headers=headers)
    ).status == 200
    assert (
        await client.patch(
            "/api/orchestrator/v1/integrations/notion",
            json={"revision": 1, "enabled": False},
            headers=headers,
        )
    ).status == 403
    good = {**headers, "Origin": origin, "X-Orchestrator-Request": "1"}
    assert (
        await client.patch(
            "/api/orchestrator/v1/integrations/notion",
            json={"revision": 1, "enabled": False},
            headers=good,
        )
    ).status == 200
    store._db.execute("UPDATE browser_sessions SET expires=0")
    assert (
        await client.get("/api/orchestrator/v1/integrations", headers=headers)
    ).status == 401
    assert (await client.get("/api/browser-session", headers=headers)).status == 200


async def test_companion_release_is_not_published(api):  # noqa: F811
    from tests.test_master_phone import pair

    client, store, _ = api
    device, token = pair(store)
    store.set_work_access(device, True)
    response = await client.get(
        "/v1/updates?channel=companion", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status == 200
    assert not (await response.json())["available"]
