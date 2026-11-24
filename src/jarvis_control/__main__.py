"""jarvis-control service and admin commands.

python -m jarvis_control serve
python -m jarvis_control pair --kind app|desktop [--master]
python -m jarvis_control devices
python -m jarvis_control revoke DEVICE_ID
python -m jarvis_control master DEVICE_ID [--off]   (the phone that gets the reports)
python -m jarvis_control sessions
python -m jarvis_control events                     (recent agent events, deliveries)
python -m jarvis_control personal-status | personal-verify [ACCOUNT]
python -m jarvis_control personal-mark ACCOUNT authorizing|disconnected
python -m jarvis_control personal-runtime on|off    (the agent's personal tools)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .backends import AgentUnit, LiveKitRooms
from .commands import CommandRunner
from .config import Settings
from .desktop import DesktopHub
from .diagnostics import Diagnostics
from .notes import NotesWriter
from .operations import Operations
from .orchestrator import Orchestrator
from .personal import N8nProxy, PersonalGateway
from .routines import DEFAULT_ROUTINES, RoutineScheduler
from .sessions import SessionManager
from .store import Store
from .web import build_internal_app, build_public_app

logger = logging.getLogger("jarvis_control")
TICK_SECONDS = 2.0
PERSONAL_ACCOUNTS = ["google_a", "google_b", "outlook", "notion"]


def release_version() -> str:
    # /opt/jarvis/releases/<version>/src/jarvis_control/__main__.py
    return os.environ.get("JARVIS_RELEASE") or Path(__file__).resolve().parents[2].name


def personal_proxy(url: str, secret: str) -> N8nProxy | None:
    return N8nProxy(url, secret) if url and secret else None


async def personal_command(args: argparse.Namespace, store: Store) -> None:
    from .integrations import CATALOG

    # Admin commands run without the service's environment (no LiveKit keys).
    proxy = personal_proxy(
        os.environ.get("JARVIS_PERSONAL_N8N_URL", "").strip(),
        os.environ.get("JARVIS_PERSONAL_N8N_SECRET", "").strip(),
    )
    gateway = PersonalGateway(store, proxy)
    try:
        if args.command == "personal-runtime":
            if args.state == "on" and proxy is None:
                raise SystemExit("set JARVIS_PERSONAL_N8N_URL and _SECRET first")
            store.integrations.set_runtime(args.state == "on")
        elif args.command == "personal-mark":
            gateway.mark(args.account, args.state)
        elif args.command == "personal-verify":
            if proxy is None:
                raise SystemExit("set JARVIS_PERSONAL_N8N_URL and _SECRET first")
            for account in [args.account] if args.account else list(CATALOG):
                result = await gateway.verify(account)
                checks = " ".join(
                    f"{k}={v}" for k, v in (result.get("services") or {}).items()
                )
                print(
                    f"{account:9} {result['state']:13} {result.get('detail') or ''} {checks}"
                )
            return
        runtime = "on" if store.integrations.runtime_enabled else "off"
        print(
            f"runtime {runtime}; gateway {'configured' if proxy else 'not configured'}"
        )
        for account in CATALOG:
            view = store.integrations.account(account)
            state = store.integrations.state(account)
            checked = _when(state["checked_at"]) if state["checked_at"] else "never"
            print(
                f"{account:9} {view['status']:14} services={','.join(view['services'])}"
                f" verified={','.join(state['services']) or '-'} checked={checked}"
                f" identity={state['identity'] or '-'} {state['detail'] or ''}"
            )
    finally:
        if proxy is not None:
            await proxy.aclose()


async def serve(settings: Settings) -> None:
    store = Store(settings.db_path)
    unit = AgentUnit(settings.agent_unit, settings.ready_file)
    rooms = LiveKitRooms(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
        settings.agent_name,
    )
    manager = SessionManager(store, unit, rooms)
    store.seed_routines(DEFAULT_ROUTINES, time.time())
    scheduler = RoutineScheduler(store.routines, manager)
    hub = DesktopHub(store)
    operations = Operations(store, hub.bus)
    hub.late_result = operations.action_result
    operations.cancel_action = hub.cancel_action
    commands = CommandRunner(store, hub, operations)
    orchestrator = Orchestrator(
        settings.db_path.parent / "orchestrator.db",
        store,
        hub,
        hub.bus,
        writer=NotesWriter.from_env(),
        require_local_runtime=True,
    )
    hub.job_update = orchestrator.record_job_update
    diagnostics = Diagnostics(
        store,
        hub,
        hub.bus,
        spool=settings.db_path.parent / "reports",
        writer=NotesWriter.from_env(),
        unit_state=unit.state,
        version=release_version(),
        runtime=operations.runtime,
    )

    public = build_public_app(
        store=store,
        manager=manager,
        hub=hub,
        rooms=rooms,
        unit_state=unit.state,
        maintenance=settings.maintenance_file.exists,
        version=release_version(),
        commands=commands,
        diagnostics=diagnostics,
        orchestrator=orchestrator,
    )

    def live_music_target() -> str | None:
        # One session at a time: the agent's music follows that phone's profile.
        for session in store.live_sessions():
            return store.master_music_target(session.device_id)
        return None

    proxy = personal_proxy(settings.personal_n8n_url, settings.personal_n8n_secret)
    internal = build_internal_app(
        hub=hub,
        token=settings.internal_token,
        music_target=live_music_target,
        store=store,
        orchestrator=orchestrator,
        personal=PersonalGateway(store, proxy),
    )

    runners = [
        web.AppRunner(public, access_log=None),
        web.AppRunner(internal, access_log=None),
    ]
    for runner in runners:
        await runner.setup()
    await web.TCPSite(runners[0], settings.public_host, settings.public_port).start()
    await web.TCPSite(
        runners[1], settings.internal_host, settings.internal_port
    ).start()
    logger.info(
        "jarvis-control %s listening on %s:%d (internal %s:%d)",
        release_version(),
        settings.public_host,
        settings.public_port,
        settings.internal_host,
        settings.internal_port,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    last_prune = last_publish = 0.0
    while not stop.is_set():
        try:
            await manager.tick()
            await scheduler.tick()
            await operations.tick()
            await orchestrator.tick()
            if time.time() - last_publish > 60:
                last_publish = time.time()
                await diagnostics.tick()
            if time.time() - last_prune > 3600:
                store.prune(time.time())
                last_prune = time.time()
        except Exception:
            logger.exception("supervisor tick failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)

    for runner in runners:
        await runner.cleanup()
    await rooms.aclose()
    store.close()


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-control")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the service")
    pair = commands.add_parser("pair", help="create a one-time pairing code")
    pair.add_argument("--kind", choices=["app", "desktop"], default="app")
    pair.add_argument("--minutes", type=float, default=10)
    pair.add_argument(
        "--master", action="store_true", help="the phone becomes the master device"
    )
    commands.add_parser("devices", help="list paired devices")
    revoke = commands.add_parser("revoke", help="revoke a paired device")
    revoke.add_argument("device_id")
    master = commands.add_parser("master", help="choose the master phone")
    master.add_argument("device_id")
    master.add_argument("--off", action="store_true", help="remove the master role")
    work = commands.add_parser(
        "work", help="grant reports/projects without the master role"
    )
    work.add_argument("device_id")
    work.add_argument("--off", action="store_true")
    personal = commands.add_parser(
        "personal-access", help="grant personal connections separately from master/work"
    )
    personal.add_argument("device_id")
    personal.add_argument("--off", action="store_true")
    commands.add_parser("personal-status", help="personal accounts and checks")
    verify = commands.add_parser(
        "personal-verify", help="check identity and services through n8n"
    )
    verify.add_argument("account", nargs="?", choices=PERSONAL_ACCOUNTS)
    mark = commands.add_parser("personal-mark", help="authorizing or disconnected")
    mark.add_argument("account", choices=PERSONAL_ACCOUNTS)
    mark.add_argument("state", choices=["authorizing", "disconnected"])
    switch = commands.add_parser(
        "personal-runtime", help="turn the agent's personal tools on or off"
    )
    switch.add_argument("state", choices=["on", "off"])
    sessions = commands.add_parser("sessions", help="show recent sessions")
    sessions.add_argument("--limit", type=int, default=15)
    events = commands.add_parser("events", help="show recent agent events")
    events.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    if args.command == "serve":
        asyncio.run(serve(Settings.from_env()))
        return

    state_dir = Path(
        os.environ.get("JARVIS_CONTROL_STATE_DIR", "/var/lib/jarvis-control")
    )
    store = Store(state_dir / "control.db")
    if args.command == "pair":
        if args.master and args.kind != "app":
            raise SystemExit("Only a phone app can be the master device.")
        code = store.create_pairing_code(
            args.kind, args.minutes * 60, time.time(), master=args.master
        )
        role = "master phone" if args.master else args.kind
        print(
            f"Pairing code for a {role}: {code}  (valid {args.minutes:g} min, single use)"
        )
    elif args.command == "devices":
        for device in store.devices():
            state = "revoked" if device.revoked_at else "active"
            role = (
                "master"
                if store.profile(device.id).master
                else "work"
                if store.can_work(device.id)
                else ""
            )
            print(
                f"{device.id}  {device.kind:7}  {state:7}  {role:6}  last seen"
                f" {_when(device.last_seen_at)}  {device.name}"
            )
    elif args.command == "revoke":
        ok = store.revoke_device(args.device_id, time.time())
        print("revoked" if ok else "no active device with that id")
    elif args.command == "master":
        ok = store.set_master(args.device_id, not args.off, time.time())
        if not ok:
            print("no active phone with that id")
        else:
            print("master role removed" if args.off else "this phone is now the master")
    elif args.command == "work":
        ok = store.set_work_access(args.device_id, not args.off)
        print("work access updated" if ok else "no active device with that id")
    elif args.command == "personal-access":
        ok = store.set_personal_access(args.device_id, not args.off)
        if not ok:
            raise SystemExit("no active device with that id")
        print("personal access removed" if args.off else "personal access granted")
    elif args.command.startswith("personal-") and args.command != "personal-access":
        asyncio.run(personal_command(args, store))
    elif args.command == "events":
        latest = store.latest_event_seq()
        for event in store.events_after(max(0, latest - args.limit), args.limit):
            stages = store.deliveries(event["id"])
            delivered = ",".join(
                stage
                for stage in ("received", "displayed", "announced", "read")
                if any(d.get(f"{stage}_at") for d in stages)
            )
            print(
                f"{_when(event['created_at'])}  {event['kind']:14}"
                f"  {event['status'] or '-':14}  {delivered or 'pending':30}"
                f"  {event['title']}"
            )
    elif args.command == "sessions":
        for session in store.recent_sessions(args.limit):
            print(
                f"{_when(session.created_at)}  {session.id[:8]}  {session.state:8}"
                f"  {session.end_reason or ''}"
            )


if __name__ == "__main__":
    main()
