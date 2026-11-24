"""Jarvis Desktop: pair this PC with the JARVIS server and keep it reachable.

    python -m jarvis_desktop pair --code ABCD-EFGH [--api https://...] [--name PC]
    python -m jarvis_desktop run              (normally started at logon; also starts
                                               the DiceVTT RAG, which stops with it)
    python -m jarvis_desktop install-startup  (start hidden at every logon)
    python -m jarvis_desktop uninstall-startup
    python -m jarvis_desktop stop
    python -m jarvis_desktop status
    python -m jarvis_desktop install-hooks    (exact approval/question states for
                                               Claude Code and Codex; uninstall-hooks)
    python -m jarvis_desktop capture on|off   (allow VS Code window screenshots)
    python -m jarvis_desktop install-orchestrator  (Antigravity may read projects
                                               with read-only commands; uninstall-…)

Run from the jarvis/src folder with the project's virtual environment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import platform
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp

from . import agy_setup, hooks_setup
from .actions import build_actions
from .agent_jobs import JobManager, active_jobs
from .capture import set_capture_enabled
from .client import DesktopClient
from .credentials import Pairing, local_dir
from .outbox import Outbox
from .planning_context import rag_context
from .rag_service import RagStack, stop_running_stack

DEFAULT_API = "https://your-jarvis-api.example.com"
STARTUP_SCRIPT = "JarvisDesktop.vbs"
logger = logging.getLogger("jarvis_desktop")


def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


async def _pair(api_url: str, code: str, name: str) -> Pairing:
    async with (
        aiohttp.ClientSession() as http,
        http.post(
            api_url.rstrip("/") + "/v1/pair",
            json={"code": code, "name": name},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp,
    ):
        body = await resp.json(content_type=None)
        if resp.status != 201:
            raise SystemExit(f"Pairing failed: {body.get('message', resp.status)}")
    if body.get("kind") != "desktop":
        raise SystemExit("That code was created for the phone app; use --kind desktop.")
    return Pairing(api_url, body["device_id"], name, body["device_token"])


def _single_instance() -> object:
    """Hold a lock file for the life of the process; exit if another copy runs."""
    import msvcrt

    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / "run.lock", "a+")  # noqa: SIM115 - held until exit
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("Jarvis Desktop is already running.") from None
    (directory / "run.pid").write_text(str(os.getpid()))
    return handle


async def _run(pairing: Pairing) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    # The DiceVTT RAG lives and dies with the helper on the primary PC (rag_service.py).
    rag = RagStack()
    # Created in the loop: it schedules tasks. Planners get the RAG's hints.
    jobs = JobManager(context=rag_context(rag))
    cleaning = asyncio.ensure_future(asyncio.to_thread(jobs.clean))
    await rag.start()
    supervisor = asyncio.ensure_future(rag.supervise(stop)) if rag.available else None
    client = DesktopClient(
        pairing.api_url,
        pairing.token,
        build_actions(jobs, rag),
        outbox=Outbox(),
        jobs=jobs,
    )
    try:
        await client.run_forever(stop)
    finally:
        stop.set()
        await asyncio.gather(cleaning, return_exceptions=True)
        if supervisor is not None:
            await asyncio.gather(supervisor, return_exceptions=True)
        await rag.shutdown()
        if rag.stopped_on_purpose:
            logger.info("helper stopped because the RAG was turned off")


def _configure_logging(to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file:
        local_dir().mkdir(parents=True, exist_ok=True)
        handlers = [
            RotatingFileHandler(
                local_dir() / "desktop.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis_desktop")
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--code", required=True)
    pair.add_argument("--api", default=DEFAULT_API)
    pair.add_argument("--name", default=platform.node() or "Windows PC")
    commands.add_parser("run")
    commands.add_parser("install-startup")
    commands.add_parser("uninstall-startup")
    stop = commands.add_parser("stop")
    stop.add_argument(
        "--force", action="store_true", help="stop even while orchestrator jobs run"
    )
    commands.add_parser("status")
    commands.add_parser("install-hooks")
    commands.add_parser("uninstall-hooks")
    commands.add_parser("install-orchestrator")
    commands.add_parser("uninstall-orchestrator")
    capture = commands.add_parser("capture")
    capture.add_argument("state", choices=["on", "off"])
    args = parser.parse_args()
    _configure_logging(
        to_file=args.command == "run" and sys.executable.endswith("pythonw.exe")
    )

    if args.command == "pair":
        pairing = asyncio.run(_pair(args.api, args.code, args.name))
        pairing.save()
        print(f"Paired as {pairing.name} ({pairing.device_id[:8]}).")
    elif args.command == "run":
        pairing = Pairing.load()
        if pairing is None:
            raise SystemExit(
                "Not paired yet: run `python -m jarvis_desktop pair --code ...`."
            )
        lock = _single_instance()
        try:
            asyncio.run(_run(pairing))
        finally:
            lock.close()  # type: ignore[attr-defined]
    elif args.command == "install-startup":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        source_dir = Path(__file__).resolve().parents[1]
        script = (
            "' Starts the JARVIS desktop helper at logon without a console window.\r\n"
            'Set shell = CreateObject("WScript.Shell")\r\n'
            f'shell.CurrentDirectory = "{source_dir}"\r\n'
            f'shell.Run """{pythonw}"" -m jarvis_desktop run", 0, False\r\n'
        )
        target = _startup_dir() / STARTUP_SCRIPT
        target.write_text(script, encoding="utf-8", newline="")
        print(f"Installed {target}")
    elif args.command == "uninstall-startup":
        target = _startup_dir() / STARTUP_SCRIPT
        target.unlink(missing_ok=True)
        print(f"Removed {target}")
    elif args.command == "stop":
        # Stopping mid-job discards work the agent may have finished (exit code 3
        # lets parar-tudo.cmd ask first).
        running = active_jobs()
        if running and not args.force:
            for job in running:
                print(f"  {job}")
            print(
                f"{len(running)} orchestrator job(s) still running; "
                "use 'stop --force' to stop anyway."
            )
            raise SystemExit(3)
        # Close the RAG gracefully first (Chroma/SQLite); the helper follows it and
        # exits on its own. Killing the helper afterwards covers older or stuck runs.
        for note in stop_running_stack():
            print(note)
        pid_file = local_dir() / "run.pid"
        if not pid_file.exists():
            raise SystemExit("Jarvis Desktop is not running.")
        with contextlib.suppress(OSError):
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
        print("Stopped.")
    elif args.command == "status":
        pairing = Pairing.load()
        print(
            f"Paired: {pairing.name} -> {pairing.api_url}" if pairing else "Not paired."
        )
        rag = RagStack()
        print("Offers:", ", ".join(sorted(build_actions(rag=rag))))
        print(
            f"RAG stack: {'installed at ' + str(rag.root) if rag.available else 'not installed'}"
        )
    elif args.command == "install-hooks":
        for note in hooks_setup.install():
            print(note)
        print(
            "Claude Code uses the hooks in sessions opened from now on. Codex skips new "
            "hooks until you trust them: run Codex in a terminal and use /hooks."
        )
    elif args.command == "uninstall-hooks":
        for note in hooks_setup.uninstall() or ["no hooks file found"]:
            print(note)
    elif args.command == "install-orchestrator":
        print(agy_setup.install())
    elif args.command == "uninstall-orchestrator":
        print(agy_setup.uninstall())
    elif args.command == "capture":
        set_capture_enabled(args.state == "on")
        print(
            f"VS Code capture is {args.state}. Restart the helper (stop, then the "
            "Startup script) so the server sees the change."
        )


if __name__ == "__main__":
    main()
