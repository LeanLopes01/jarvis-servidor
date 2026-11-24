"""Routines (server scheduling and API), task notifications, weather and news."""

import json
from datetime import datetime

import pytest

from src.jarvis_control.routines import (
    DEFAULT_ROUTINES,
    TIMEZONE,
    RoutineError,
    RoutineScheduler,
    next_run,
    validate,
)
from src.web_info import parse_feed
from tests.test_control import Clock, FakeRooms, FakeUnit, api, parts  # noqa: F401 - fixtures reuse  # noqa: F401 - pytest fixtures


def at(text: str) -> float:
    """Epoch seconds for a GMT-3 wall-clock time, e.g. "2026-10-17 05:59"."""
    return (
        datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE).timestamp()
    )


def test_validate_routine() -> None:
    routine = validate(
        {"name": "Gym", "time": "07:30", "days": [0, 2, 4, 2], "prompt": "Motivate me."}
    )
    assert routine["days"] == [0, 2, 4]
    assert routine["enabled"] is True
    for bad in (
        {"name": "", "time": "07:30", "days": [0], "prompt": "x"},
        {"name": "x", "time": "7:30", "days": [0], "prompt": "x"},
        {"name": "x", "time": "24:00", "days": [0], "prompt": "x"},
        {"name": "x", "time": "07:30", "days": [7], "prompt": "x"},
        {"name": "x", "time": "07:30", "days": [], "prompt": "x"},
        {"name": "x", "time": "07:30", "days": [0], "prompt": ""},
    ):
        with pytest.raises(RoutineError):
            validate(bad)


def test_next_run_in_gmt_minus_3() -> None:
    wake = DEFAULT_ROUTINES[0]
    # Saturday 17 Oct 2026, 05:59 in Lorena -> today 06:00.
    assert next_run(wake, at("2026-10-17 05:59")) == at("2026-10-17 06:00")
    assert next_run(wake, at("2026-10-17 06:00")) == at("2026-10-18 06:00")
    weekdays = {**wake, "days": [0, 1, 2, 3, 4]}
    assert next_run(weekdays, at("2026-10-17 07:00")) == at(
        "2026-10-19 06:00"
    )  # Monday
    assert next_run({**wake, "enabled": False}, at("2026-10-17 05:00")) is None


async def test_scheduler_prewarms_once_and_keeps_agent_up(parts) -> None:  # noqa: F811
    clock, store, unit, _, manager = parts
    store.seed_routines(DEFAULT_ROUTINES, clock.now)
    store.seed_routines(DEFAULT_ROUTINES, clock.now)  # only once
    assert len(store.routines()) == 1
    scheduler = RoutineScheduler(store.routines, manager, clock=clock)

    clock.now = at("2026-10-17 05:58")
    await scheduler.tick()
    assert unit.starts == 0
    clock.now = at("2026-10-17 05:59") + 20
    await scheduler.tick()
    await scheduler.tick()
    assert unit.starts == 1
    # No session yet, but the agent stays up for the routine instead of the idle grace.
    clock.now = at("2026-10-17 06:03")
    await manager.tick()
    assert unit.stops == 0
    clock.now = at("2026-10-17 06:06")
    await manager.tick()
    assert unit.stops == 1


async def test_routine_api_and_session_context(api, parts) -> None:  # noqa: F811
    client, store, _unit = api
    clock, *_, _manager = parts
    store.seed_routines(DEFAULT_ROUTINES, clock.now)
    code = store.create_pairing_code("app", 600, 2_000_000_000)
    token = (await (await client.post("/v1/pair", json={"code": code})).json())[
        "device_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}

    listed = await (await client.get("/v1/routines", headers=headers)).json()
    assert [r["id"] for r in listed["routines"]] == ["wake-up"]
    created = await client.post(
        "/v1/routines",
        headers=headers,
        json={
            "name": "Gym",
            "time": "18:00",
            "days": [1, 3],
            "prompt": "Remind me of the gym.",
        },
    )
    assert created.status == 201
    gym = await created.json()
    edited = await client.put(
        f"/v1/routines/{gym['id']}", headers=headers, json={**gym, "enabled": False}
    )
    assert (await edited.json())["enabled"] is False
    bad = await client.put("/v1/routines/wake-up", headers=headers, json={"name": "x"})
    assert bad.status == 400
    assert (
        await client.delete(f"/v1/routines/{gym['id']}", headers=headers)
    ).status == 204

    session = await (
        await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "routine_id": "wake-up",
                "location": {"lat": -22.7305, "lon": -45.1247},
            },
        )
    ).json()
    context = json.loads(store.session(session["id"]).context)
    assert context["routine"]["name"] == "Wake up"
    assert context["location"] == {"lat": round(-22.7305, 3), "lon": round(-45.1247, 3)}
    unknown = await client.post(
        "/v1/sessions", headers=headers, json={"routine_id": "nope"}
    )
    assert unknown.status == 400

    store.add_notification(
        "agent_finished", "Claude Code finished · jarvis", "Done.", 1
    )
    store.add_notification("agent_finished", "Codex finished · site", "Done.", 2)
    first = await (
        await client.get("/v1/notifications?after=0", headers=headers)
    ).json()
    assert next(n["title"] for n in first["notifications"]).startswith("Claude Code")
    later = await (
        await client.get(
            f"/v1/notifications?after={first['notifications'][0]['id']}",
            headers=headers,
        )
    ).json()
    assert len(later["notifications"]) == 1


def test_news_feed_parsing() -> None:
    xml = b"""<rss><channel>
      <item><title>Chuva forte atinge o Vale - G1</title><source>G1</source>
        <pubDate>Sat, 17 Oct 2026 08:00:00 GMT</pubDate></item>
      <item><title>Nova vacina aprovada - Folha</title><source>Folha</source></item>
    </channel></rss>"""
    items = parse_feed(xml, limit=5)
    assert items[0] == {
        "headline": "Chuva forte atinge o Vale",
        "source": "G1",
        "published": "2026-10-17 08:00",
    }
    assert items[1]["published"] is None


def test_agent_reads_session_context() -> None:
    from src.agent import session_context

    meta = json.dumps(
        {
            "routine": {"name": "Wake up", "prompt": "Brief me."},
            "location": {"lat": 1, "lon": 2},
        }
    )
    assert session_context(meta)["routine"]["name"] == "Wake up"
    assert session_context(None) == {}
    assert session_context("not json") == {}
