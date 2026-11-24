"""The DiceVTT RAG stack on the primary PC, tied to this helper's life.

`run` starts the RAG service (unity_rag, 127.0.0.1:8767), the ingestion service
(rag_ingestion, 127.0.0.1:8766) and, when .local/diagnostics.toml exists, the report runner
(dicevtt_diagnostics, 127.0.0.1:8768) from the Orquestrador's .venv-rag. Stopping the helper
stops all of them. Stopping the RAG on purpose (`unity_rag stop`, the Orquestrador UI) ends
with exit code 0, and the helper then stops too. A crash is restarted a few times.

The services run in a Windows job object that kills them when the helper's handle
closes, so a helper terminated by `jarvis_desktop stop` never leaves them orphaned.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("jarvis_desktop")

DEFAULT_ROOT = r"D:\Programação\ProjetoLocais\Orquestrador"
RESTART_LIMIT = 3
RESTART_WINDOW = 600.0
HEALTH_TIMEOUT = 5.0
LOG_LIMIT = 5_000_000


def orchestrator_root() -> Path:
    return Path(os.environ.get("JARVIS_RAG_ROOT", DEFAULT_ROOT))


@dataclass
class Service:
    name: str
    module: str
    config: Path
    port: int
    health: str
    service_id: str
    token_file: Path
    log_file: Path
    optional: bool = False  # without its config the rest of the stack still starts
    process: subprocess.Popen | None = None
    adopted: bool = False
    restarts: list[float] = field(default_factory=list)
    state: str = "stopped"
    error: str | None = None

    def token(self) -> str:
        try:
            return self.token_file.read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


class _KillOnCloseJob:
    """Windows job object: every service dies when the helper's handle closes."""

    def __init__(self) -> None:
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = Extended()
        info.BasicLimitInformation.LimitFlags = (
            0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.handle = handle
            self._kernel32 = kernel32

    def assign(self, process: subprocess.Popen) -> None:
        handle = int(process._handle)  # type: ignore[attr-defined]
        if self.handle is not None and not self._kernel32.AssignProcessToJobObject(
            self.handle, handle
        ):
            logger.warning("could not tie %s to the helper's job object", process.pid)


class RagStack:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or orchestrator_root()
        local = self.root / ".local"
        self.python = self.root / ".venv-rag" / "Scripts" / "python.exe"
        self.services = {
            "rag": Service(
                "rag",
                "unity_rag",
                local / "rag.toml",
                8767,
                "/v1/rag/health",
                "unity-rag",
                local / "rag" / "api-token",
                local / "rag" / "logs" / "service.log",
            ),
            "ingestion": Service(
                "ingestion",
                "rag_ingestion",
                local / "ingestion.toml",
                8766,
                "/v1/ingestion/health",
                "rag-ingestion",
                local / "ingestion" / "api-token",
                local / "ingestion" / "logs" / "service.log",
            ),
            "diagnostics": Service(
                "diagnostics",
                "dicevtt_diagnostics",
                local / "diagnostics.toml",
                8768,
                "/v1/diagnostics/health",
                "dicevtt-diagnostics",
                local / "audits" / "api-token",
                local / "audits" / "logs" / "service.log",
                optional=True,
            ),
        }
        self._job: _KillOnCloseJob | None = None
        self.stopped_on_purpose = False

    @property
    def available(self) -> bool:
        return self.python.exists() and all(
            s.config.exists() for s in self.services.values() if not s.optional
        )

    # HTTP to the local services -------------------------------------------------------

    async def request(
        self,
        name: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, Any]]:
        service = self.services[name]
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http,
            http.request(
                method,
                service.url(path),
                json=body,
                allow_redirects=False,
                headers={"X-Local-Token": service.token()},
            ) as response,
        ):
            data = await response.json(content_type=None)
            return response.status, data if isinstance(data, dict) else {"data": data}

    async def _healthy(self, service: Service) -> bool:
        try:
            status, data = await self.request(
                service.name, "GET", service.health, timeout=HEALTH_TIMEOUT
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
            return False
        return status == 200 and data.get("service") == service.service_id

    async def _port_open(self, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), 1.0
            )
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    # Life cycle ------------------------------------------------------------------------------

    def _spawn(self, service: Service) -> None:
        service.log_file.parent.mkdir(parents=True, exist_ok=True)
        if service.log_file.exists() and service.log_file.stat().st_size > LOG_LIMIT:
            service.log_file.replace(service.log_file.with_suffix(".log.1"))
        log = open(service.log_file, "ab")  # noqa: SIM115 - owned by the child
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        service.process = subprocess.Popen(
            [
                str(self.python),
                "-m",
                service.module,
                "--config",
                str(service.config),
                "serve",
            ],
            cwd=str(self.root),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            env=env,
        )
        log.close()
        if self._job is None:
            self._job = _KillOnCloseJob()
        self._job.assign(service.process)
        service.adopted = False
        service.state, service.error = "starting", None
        logger.info("started %s service (pid %s)", service.name, service.process.pid)

    async def start(self) -> None:
        if not self.available:
            logger.info("RAG stack not installed at %s; skipping", self.root)
            return
        for service in self.services.values():
            if service.optional and not service.config.exists():
                logger.info(
                    "%s service not configured (%s); skipping",
                    service.name,
                    service.config,
                )
                continue
            await self._start_one(service)

    async def _start_one(self, service: Service) -> None:
        if await self._healthy(service):
            service.adopted, service.state = True, "running"
            logger.info(
                "%s service already running; the helper will stop it on exit",
                service.name,
            )
            return
        if await self._port_open(service.port):
            service.state = "error"
            service.error = (
                f"port {service.port} is used by another program; nothing was stopped"
            )
            logger.warning("%s: %s", service.name, service.error)
            return
        self._spawn(service)

    async def supervise(self, stop: asyncio.Event, interval: float = 3.0) -> None:
        """Watch the services; a clean exit of the RAG stops the helper."""
        while not stop.is_set():
            for service in self.services.values():
                await self._check(service, stop)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)

    async def _check(self, service: Service, stop: asyncio.Event) -> None:
        process = service.process
        if process is not None:
            code = process.poll()
            if code is None:
                if service.state == "starting" and await self._healthy(service):
                    service.state = "running"
                return
            service.process = None
            if code == 0:
                service.state = "stopped"
                logger.info("%s service stopped on request", service.name)
                if service.name == "rag":
                    logger.info("the RAG was turned off: stopping the helper as well")
                    self.stopped_on_purpose = True
                    stop.set()
                return
            await self._restart(service, f"exited with code {code}")
        elif service.adopted and not await self._healthy(service):
            service.adopted = False
            if service.name == "rag":
                logger.info(
                    "the adopted RAG service went away: stopping the helper as well"
                )
                self.stopped_on_purpose = True
                stop.set()
                return
            await self._restart(service, "stopped responding")

    async def _restart(self, service: Service, reason: str) -> None:
        now = time.monotonic()
        service.restarts = [t for t in service.restarts if now - t < RESTART_WINDOW]
        if len(service.restarts) >= RESTART_LIMIT:
            service.state, service.error = (
                "error",
                f"{reason}; restart limit reached, see {service.log_file}",
            )
            logger.error("%s service %s", service.name, service.error)
            return
        service.restarts.append(now)
        logger.warning("%s service %s; restarting", service.name, reason)
        await self._start_one(service)

    async def shutdown(self, timeout: float = 20.0) -> None:
        """Graceful stop (closes Chroma/SQLite), then terminate whatever is left."""
        for service in self.services.values():
            if service.process is None and not service.adopted:
                continue
            with contextlib.suppress(
                aiohttp.ClientError, TimeoutError, ValueError, OSError
            ):
                await self.request(
                    service.name,
                    "POST",
                    service.health.replace("health", "shutdown"),
                    {},
                    timeout=5,
                )
        deadline = time.monotonic() + timeout
        for service in self.services.values():
            process = service.process
            if process is not None:
                while process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.25)
                if process.poll() is None:
                    process.terminate()
                    logger.warning(
                        "%s service did not stop in time; terminated", service.name
                    )
            service.process, service.adopted, service.state = None, False, "stopped"

    def status(self) -> dict[str, Any]:
        return {
            name: {
                "state": s.state,
                "error": s.error,
                "pid": s.process.pid if s.process else None,
                "adopted": s.adopted,
                "port": s.port,
            }
            for name, s in self.services.items()
        }


def stop_running_stack(root: Path | None = None, timeout: float = 20.0) -> list[str]:
    """For `jarvis_desktop stop`: ask both services to shut down before the helper is killed."""
    stack = RagStack(root)
    notes: list[str] = []

    async def run() -> None:
        for service in stack.services.values():
            if await stack._healthy(service):
                with contextlib.suppress(
                    aiohttp.ClientError, TimeoutError, ValueError, OSError
                ):
                    await stack.request(
                        service.name,
                        "POST",
                        service.health.replace("health", "shutdown"),
                        {},
                        5,
                    )
                deadline = time.monotonic() + timeout
                while (
                    await stack._port_open(service.port) and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.5)
                notes.append(f"{service.name} stopped")

    if stack.available:
        asyncio.run(run())
    return notes
