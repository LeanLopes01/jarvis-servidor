"""Reports about JARVIS itself: "Jarvis, write a report on how you are working".

Runs inside jarvis-control, not in the voice agent, and needs no AI model, so it
works when the call or Gemini is stuck. Each source is collected in parallel with
its own time limit; a missing source (PC offline, journal not readable) is listed
as a gap instead of blocking the rest. The result is two files whose names are only
the São Paulo timestamp, in JARVIS-Notas/Relatorios:

    2026-10-18T16-42-08.123456-0300.md    readable, factual summary
    2026-10-18T16-42-08.123456-0300.zip   the evidence, with a manifest

Secrets are removed before anything is written. Until Nextcloud confirms an upload
the files stay in a private spool on the server and are published later.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .desktop import DesktopHub, DesktopOfflineError, DesktopTimeoutError
from .events import EventBus
from .notes import NotesError, NotesWriter
from .routines import TIMEZONE
from .store import Store

logger = logging.getLogger("jarvis_control")

UNITS = ("jarvis-control.service", "jarvis-agent.service")
DEFAULT_MINUTES = 30
MAX_MINUTES = 24 * 60
SOURCE_BYTES = 4_000_000
LOG_LINES = 20_000
SOURCE_TIMEOUT = 25.0

_SECRET_LABEL = re.compile(
    r"(?i)\b(senha|password|passwd|pwd|secret|token|api[_-]?key|authorization)\b"
    r"((?:[^\n]{0,15}?[:=])|\s+bearer)\s*['\"]?([^\s'\",;]{3,})"
)
_SECRET_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|jd1\.[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|AIza[0-9A-Za-z_-]{30,}|[A-Za-z0-9+/_-]{48,})"
)


def redact(text: str) -> tuple[str, int]:
    """Text without secrets, and how many were removed."""
    text, labelled = _SECRET_LABEL.subn(
        lambda m: f"{m.group(1)}{m.group(2)} [removed]", text
    )
    text, shaped = _SECRET_SHAPES.subn("[removed]", text)
    return text, labelled + shaped


def report_name(now: float) -> str:
    """Only the timestamp: microseconds and the São Paulo offset."""
    local = datetime.fromtimestamp(now, TIMEZONE)
    return f"{local:%Y-%m-%dT%H-%M-%S}.{local.microsecond:06d}{local:%z}"


def _local(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


class Collection:
    """The files of one report, with what happened to each source."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.sources: list[dict[str, Any]] = []
        self.redactions = 0

    def add(self, path: str, content: str | bytes | Any) -> None:
        if not isinstance(content, (str, bytes)):
            content = json.dumps(content, ensure_ascii=False, indent=1, default=str)
        if isinstance(content, str):
            content, removed = redact(content)
            self.redactions += removed
            content = content.encode("utf-8")
        truncated = len(content) > SOURCE_BYTES
        self.files[path] = content[:SOURCE_BYTES]
        if truncated:
            self.files[path] += b"\n[truncated at the size limit]\n"


async def _run(*args: str, timeout: float = 20) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await asyncio.shield(proc.wait())
        raise
    text = out.decode("utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError(f"{args[0]} exited with {proc.returncode}: {text[-400:]}")
    return text


class Diagnostics:
    def __init__(
        self,
        store: Store,
        hub: DesktopHub,
        bus: EventBus,
        *,
        spool: Path,
        writer: NotesWriter | None,
        unit_state: Callable[[], Awaitable[str]] | None = None,
        version: str = "unknown",
        runtime: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._hub = hub
        self._bus = bus
        self._spool = spool
        self._writer = writer
        self._unit_state = unit_state
        self._version = version
        self._runtime = runtime
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()

    # Starting a report

    def start(self, device_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Reserve the timestamp and collect in the background; returns at once."""
        request_id = str(body.get("request_id") or "")[:80]
        if request_id:
            for existing in self._store.reports(100):
                if (existing.get("summary") or {}).get("request_id") == request_id:
                    return existing  # the same request sent twice
        try:
            minutes = int(body.get("minutes") or DEFAULT_MINUTES)
        except (TypeError, ValueError):
            minutes = DEFAULT_MINUTES
        minutes = min(max(minutes, 5), MAX_MINUTES)
        note = str(body.get("note") or "").strip()
        report_id = uuid.uuid4().hex
        now = self._clock()
        name = report_name(now)
        while not self._store.reserve_report(report_id, name, device_id, note, now):
            now += 0.000001  # never reuse or overwrite a name
            name = report_name(now)
        self._store.update_report(
            report_id, summary={"request_id": request_id, "minutes": minutes}
        )
        task = asyncio.ensure_future(
            self._collect(
                report_id, name, device_id, note, minutes, now, body.get("phone")
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        report = self._store.report(report_id)
        assert report is not None
        return report

    # Collection

    async def _collect(
        self,
        report_id: str,
        name: str,
        device_id: str | None,
        note: str,
        minutes: int,
        now: float,
        phone: Any,
    ) -> None:
        since = now - minutes * 60
        bundle = Collection()
        sources: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("server state", lambda: self._server_state(bundle, since, now, device_id)),
            ("conversation", lambda: self._conversation(bundle, since)),
            ("services", lambda: self._services(bundle)),
            ("infrastructure", lambda: self._infrastructure(bundle)),
            ("BATCOMPUTADOR", lambda: self._desktop(bundle, since)),
            ("phone", lambda: self._phone(bundle, phone)),
        ]
        sources += [
            (f"journal {unit}", lambda u=unit: self._journal(bundle, u, since))
            for unit in UNITS
        ]

        async def guarded(
            label: str, collect: Callable[[], Awaitable[None]]
        ) -> dict[str, Any]:
            started = time.monotonic()
            try:
                await asyncio.wait_for(collect(), SOURCE_TIMEOUT)
                status, error = "collected", None
            except asyncio.TimeoutError:
                status, error = "timeout", f"no answer within {SOURCE_TIMEOUT:.0f} s"
            except Exception as exc:  # each source fails on its own
                status, error = "unavailable", f"{type(exc).__name__}: {exc}"[:400]
            return {
                "source": label,
                "status": status,
                "error": error,
                "seconds": round(time.monotonic() - started, 2),
            }

        results = await asyncio.gather(*(guarded(label, fn) for label, fn in sources))
        bundle.sources = list(results)
        missing = [r for r in results if r["status"] != "collected"]
        state = "partial" if missing else "ready"
        until = self._clock()
        markdown = self._markdown(name, note, minutes, since, until, bundle, state)
        archive = self._archive(name, bundle, minutes, since, until)
        folder = self._spool / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.md").write_text(markdown, encoding="utf-8")
        (folder / f"{name}.zip").write_bytes(archive)
        summary = {
            **(self._store.report(report_id) or {}).get("summary", {}),
            "sources": bundle.sources,
            "redactions": bundle.redactions,
            "md_bytes": len(markdown.encode()),
            "zip_bytes": len(archive),
        }
        self._store.update_report(
            report_id, state=state, finished_at=until, summary=summary
        )
        await self._publish(report_id)

    async def _server_state(
        self, bundle: Collection, since: float, now: float, device_id: str | None
    ) -> None:
        store = self._store
        bundle.add(
            "server/state.json",
            {
                "release": self._version,
                "agent_unit": await self._unit_state() if self._unit_state else None,
                "desktop": self._hub.status(),
                "runtime": self._runtime(device_id)
                if self._runtime and device_id
                else None,
                "sessions": store.sessions_between(since, now),
                "operations": store.operations(since=since, limit=500),
                "commands": store.table_since("commands", "created_at", since),
                "desktop_actions": store.table_since(
                    "desktop_actions", "created_at", since
                ),
                "events": [
                    {k: v for k, v in e.items() if k != "report"}
                    for e in store.table_since("events", "created_at", since)
                ],
            },
        )

    async def _conversation(self, bundle: Collection, since: float) -> None:
        items = self._store.conversation_items(since, self._clock())
        bundle.add("conversations/items.json", items)
        lines = [f"# Conversation since {_local(since)}", ""]
        for item in items:
            who = item["role"] or item["kind"]
            text = item["text"] or json.dumps(item["data"], ensure_ascii=False)[:600]
            lines.append(f"- {_local(item['at'])} [{item['room']}] **{who}**: {text}")
        if not items:
            lines.append("No conversation was recorded in this period.")
        bundle.add("conversations/conversation.md", "\n".join(lines) + "\n")

    async def _services(self, bundle: Collection) -> None:
        props = "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,ExecMainStatus,ExecMainStartTimestamp,MemoryCurrent,Result"
        out = {
            unit: await _run("systemctl", "show", unit, f"--property={props}")
            for unit in UNITS
        }
        bundle.add(
            "server/services.txt", "\n\n".join(f"[{u}]\n{t}" for u, t in out.items())
        )

    async def _journal(self, bundle: Collection, unit: str, since: float) -> None:
        # Needs the systemd-journal group (see deploy/systemd/jarvis-control.service).
        text = await _run(
            "journalctl",
            "-u",
            unit,
            "--since",
            f"@{int(since)}",
            "--no-pager",
            "-o",
            "short-iso-precise",
            "-n",
            str(LOG_LINES),
            timeout=25,
        )
        if (
            "No journal files were opened" in text
            or "insufficient permissions" in text.lower()
        ):
            raise PermissionError("the journal is not readable by jarvis-control")
        bundle.add(f"server/journal-{unit.replace('.service', '')}.log", text)

    async def _infrastructure(self, bundle: Collection) -> None:
        info: dict[str, Any] = {
            "release": self._version,
            "collected_at": _local(self._clock()),
        }
        for name, path in {
            "loadavg": "/proc/loadavg",
            "uptime": "/proc/uptime",
        }.items():
            try:
                info[name] = Path(path).read_text().strip()
            except OSError:
                info[name] = None
        try:
            meminfo = Path("/proc/meminfo").read_text().splitlines()[:5]
            info["memory"] = meminfo
        except OSError:
            info["memory"] = None
        usage = shutil.disk_usage(self._spool if self._spool.exists() else Path("."))
        info["disk_free_gb"] = round(usage.free / 1e9, 1)
        info["pid"] = os.getpid()
        bundle.add("server/infrastructure.json", info)

    async def _desktop(self, bundle: Collection, since: float) -> None:
        try:
            result = await self._hub.call(
                "diagnostics.snapshot", {"since": since}, 20.0
            )
        except DesktopOfflineError:
            raise ConnectionError("the computer is not connected") from None
        except DesktopTimeoutError:
            raise TimeoutError("the computer did not answer") from None
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error")))
        snapshot = dict(result.get("result") or {})
        log = snapshot.pop("log", "")
        bundle.add("desktop/snapshot.json", snapshot)
        bundle.add("desktop/desktop.log", log)

    async def _phone(self, bundle: Collection, phone: Any) -> None:
        if not isinstance(phone, dict):
            raise ValueError("the phone did not send its state")
        log = phone.get("log")
        bundle.add("phone/state.json", {k: v for k, v in phone.items() if k != "log"})
        bundle.add(
            "phone/app.log", "\n".join(log) if isinstance(log, list) else str(log or "")
        )

    # Output

    def _markdown(
        self,
        name: str,
        note: str,
        minutes: int,
        since: float,
        until: float,
        bundle: Collection,
        state: str,
    ) -> str:
        server = json.loads(bundle.files.get("server/state.json", b"{}") or b"{}")
        timeline: list[tuple[float, str]] = []
        for s in server.get("sessions", []):
            timeline.append(
                (s["created_at"], f"call {s['id'][:8]} started ({s['state']})")
            )
            if s.get("ended_at"):
                timeline.append(
                    (s["ended_at"], f"call {s['id'][:8]} ended: {s.get('end_reason')}")
                )
        for op in server.get("operations", []):
            timeline.append(
                (op["created_at"], f'operation {op["kind"]} "{op["label"]}" accepted')
            )
            if op.get("finished_at"):
                timeline.append(
                    (
                        op["finished_at"],
                        f"operation {op['kind']} → {op['state']} (stage: {op.get('stage')})",
                    )
                )
        for action in server.get("desktop_actions", []):
            timeline.append(
                (
                    action["created_at"],
                    f"PC action {action['action']} → {action['state']}",
                )
            )
        for event in server.get("events", []):
            timeline.append(
                (event["created_at"], f"event {event['kind']}: {event['title']}")
            )
        conversation = json.loads(
            bundle.files.get("conversations/items.json", b"[]") or b"[]"
        )
        for item in conversation:
            if item["kind"] in {"message", "tool", "error", "state"}:
                text = (item["text"] or json.dumps(item["data"], ensure_ascii=False))[
                    :160
                ]
                timeline.append((item["at"], f"{item['role'] or item['kind']}: {text}"))
        timeline.sort()

        problems: list[str] = []
        for path, content in bundle.files.items():
            if not path.endswith(".log"):
                continue
            for line in content.decode("utf-8", errors="replace").splitlines():
                if re.search(
                    r"\b(ERROR|CRITICAL|Traceback|FATAL|Exception)\b|\"level\": \"(ERROR|WARNING)\"",
                    line,
                ):
                    problems.append(f"{path}: {line[:300]}")
        lines = [
            f"# JARVIS report {name}",
            "",
            f"- **Collected:** {_local(until)} (São Paulo, UTC-03:00)",
            f"- **Period covered:** {_local(since)} to {_local(until)} ({minutes} min)",
            f"- **What the user reported:** {note or 'not stated'}",
            f"- **Backend release:** {self._version}",
            f"- **Collection:** {state} — evidence in `{name}.zip`",
            "",
            "## Sources",
            "",
            "| Source | Status | Time | Problem |",
            "| --- | --- | --- | --- |",
        ]
        for s in bundle.sources:
            lines.append(
                f"| {s['source']} | {s['status']} | {s['seconds']} s | {s['error'] or ''} |"
            )
        lines += [
            "",
            f"Secrets removed before saving: {bundle.redactions}.",
            "",
            "## Timeline (facts recorded by the server)",
            "",
        ]
        lines += [f"- {_local(at)} — {text}" for at, text in timeline[-300:]] or [
            "Nothing was recorded in this period."
        ]
        lines += ["", "## Errors and warnings found in the logs", ""]
        lines += [f"- `{p}`" for p in problems[-80:]] or [
            "None found in the collected logs."
        ]
        lines += [
            "",
            "## Interpretation",
            "",
            "Not generated: this report holds collected facts only. Sources listed as"
            " unavailable are gaps, not evidence that nothing happened there; logs and"
            " conversations that were never recorded cannot be recovered.",
            "",
        ]
        return "\n".join(lines)

    def _archive(
        self, name: str, bundle: Collection, minutes: int, since: float, until: float
    ) -> bytes:
        manifest = {
            "report": name,
            "period": {"from": _local(since), "to": _local(until), "minutes": minutes},
            "sources": bundle.sources,
            "redactions": bundle.redactions,
            "files": [
                {
                    "path": path,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for path, data in sorted(bundle.files.items())
            ],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, data in sorted(bundle.files.items()):
                archive.writestr(path, data)
            archive.writestr(
                "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1)
            )
        return buffer.getvalue()

    # Publishing to JARVIS-Notas/Relatorios

    async def _publish(self, report_id: str) -> bool:
        report = self._store.report(report_id)
        if report is None or report["published"]:
            return True
        name = report["name"]
        folder = self._spool / name
        if self._writer is None:
            self._store.update_report(
                report_id,
                error="Nextcloud is not configured for jarvis-control; kept in the spool.",
            )
            return False
        try:
            for suffix in (".md", ".zip"):
                data = (folder / f"{name}{suffix}").read_bytes()
                try:
                    await self._writer.put(f"Relatorios/{name}{suffix}", data)
                except NotesError as exc:
                    if "already exists" not in str(exc):
                        raise
        except (NotesError, OSError, Exception) as exc:  # retried by tick()
            logger.warning("report %s not published yet: %s", name, exc)
            self._store.update_report(
                report_id, error=f"not published yet: {exc}"[:400]
            )
            return False
        self._store.update_report(report_id, published=1, error=None)
        row = self._store.add_server_event(
            {
                "id": f"report-{report_id}"[:80],
                "kind": "diagnostic_ready",
                "status": "completed" if report["state"] == "ready" else "failed",
                "title": f"JARVIS report {name}",
                "summary": f"Saved in JARVIS-Notas/Relatorios ({report['state']}).",
                "data": {
                    "report_id": report_id,
                    "name": name,
                    "state": report["state"],
                },
            },
            self._clock(),
        )
        self._bus.publish(row)
        return True

    async def tick(self) -> None:
        """Publish reports that were kept in the spool (Nextcloud was down)."""
        for report in self._store.unpublished_reports():
            await self._publish(report["id"])

    def file(self, report_id: str, suffix: str) -> Path | None:
        report = self._store.report(report_id)
        if report is None or suffix not in {".md", ".zip"}:
            return None
        path = self._spool / report["name"] / f"{report['name']}{suffix}"
        return path if path.exists() else None
