import pytest

from src.jarvis_control.integrations import IntegrationError
from src.jarvis_control.store import Store
from tests.test_control import api, parts  # noqa: F401
from tests.test_master_phone import pair


def test_persistence_and_no_credentials_or_fake_connection(tmp_path):
    path = tmp_path / "control.db"
    store = Store(path)
    device, _ = pair(store)
    store.set_personal_access(device, True)
    store.integrations.update("google_a", {"revision": 1, "label": "Pessoal"})
    store.close()
    store = Store(path)
    view = store.integrations.view()
    assert store.can_personal(device)
    assert view["accounts"][0]["label"] == "Pessoal"
    assert view["accounts"][0]["revision"] == 2
    assert not view["runtime_enabled"]
    assert all(a["status"] == "setup_required" for a in view["accounts"])
    assert not {"token", "credential_id", "email", "access_token"} & set(
        view["accounts"][0]
    )
    store.close()


def test_gmail_selection_conflict_and_stale_edit_are_atomic():
    store = Store(":memory:")
    with pytest.raises(IntegrationError) as exc:
        store.integrations.update("google_b", {"revision": 1, "services": ["gmail"]})
    assert exc.value.code == "gmail_account_conflict"
    store.integrations.update(
        "google_a", {"revision": 1, "services": ["calendar", "drive"]}
    )
    store.integrations.update(
        "google_b", {"revision": 1, "services": ["calendar", "drive", "gmail"]}
    )
    with pytest.raises(IntegrationError) as exc:
        store.integrations.update("google_a", {"revision": 1, "label": "stale"})
    assert exc.value.status == 409
    assert store.integrations.view()["accounts"][0]["label"] == "Google A"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"revision": True},
        {"revision": 1, "enabled": "yes"},
        {"revision": 1, "access_token": "secret"},
        {"revision": 1, "status": "connected"},
        {"revision": 1, "label": "\n"},
        {"revision": 1, "label": "a" * 61},
        {"revision": 1, "services": ["mail"]},
        {"revision": 1, "services": []},
        {"revision": 1, "services": ["drive", "drive"]},
        {"revision": 1, "services": [{}]},
    ],
)
def test_invalid_input_cannot_modify_preferences(body):
    store = Store(":memory:")
    before = store.integrations.view()
    with pytest.raises(IntegrationError):
        store.integrations.update("google_a", body)
    assert store.integrations.view() == before


async def test_http_access_is_independent_from_master_and_work(api):  # noqa: F811
    client, store, _ = api
    device, token = pair(store, master=True)
    store.set_work_access(device, True)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/v1/integrations")).status == 401
    assert (await client.get("/v1/integrations", headers=headers)).status == 403
    assert (
        await client.patch(
            "/v1/integrations/google_a",
            headers=headers,
            json={"revision": 1, "enabled": False},
        )
    ).status == 403
    store.set_personal_access(device, True)
    response = await client.get("/v1/integrations", headers=headers)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len((await response.json())["accounts"]) == 4
    response = await client.patch(
        "/v1/integrations/google_a",
        headers=headers,
        json={"revision": 1, "enabled": False},
    )
    assert response.status == 200
    assert (await response.json())["accounts"][0]["status"] == "paused"
    response = await client.patch(
        "/v1/integrations/google_a",
        headers=headers,
        json={"revision": 1, "label": "outdated"},
    )
    assert response.status == 409
    assert (await response.json())["error"] == "revision_conflict"
    assert (
        await client.patch(
            "/v1/integrations/unknown", headers=headers, json={"revision": 1}
        )
    ).status == 404
    store.set_personal_access(device, False)
    assert (await client.get("/v1/integrations", headers=headers)).status == 403
    desktop, desktop_token = pair(store, "desktop")
    assert (
        await client.get(
            "/v1/integrations", headers={"Authorization": f"Bearer {desktop_token}"}
        )
    ).status == 403
    store.set_personal_access(desktop, True)
    assert (
        await client.get(
            "/v1/integrations", headers={"Authorization": f"Bearer {desktop_token}"}
        )
    ).status == 200
    store.set_personal_access(desktop, False)
    assert (
        await client.get(
            "/v1/integrations", headers={"Authorization": f"Bearer {desktop_token}"}
        )
    ).status == 403


def test_revoked_device_loses_access():
    store = Store(":memory:")
    device, _ = pair(store)
    store.set_personal_access(device, True)
    store.revoke_device(device, 2_000_000_001)
    assert not store.can_personal(device)
    assert not store.set_personal_access(device, True)
