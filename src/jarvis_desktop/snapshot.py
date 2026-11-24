"""What this PC can tell a JARVIS diagnostics report: the helper's own log for the
period, its outbox, and how long it has been running. Read-only; the server removes
secrets before anything is saved."""

from __future__ import annotations

import contextlib
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .credentials import local_dir
from .outbox import Outbox

LOG_LIMIT = 2_000_000
STARTED = time.time()


def log_since(since: float, directory: Path | None = None) -> str:
    """Lines of desktop.log (and its rotated copies) written after `since`."""
    folder = directory or local_dir()
    files = sorted(folder.glob("desktop.log*"), key=lambda p: p.stat().st_mtime)
    lines: list[str] = []
    for path in files:
        if path.stat().st_mtime < since:
            continue
        keep = False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp = line[:23]
            # A continuation line (traceback) follows the last decision.
            with contextlib.suppress(ValueError):
                keep = (
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                    >= since
                )
            if keep:
                lines.append(line)
    text = "\n".join(lines)
    return text[-LOG_LIMIT:]


def snapshot(since: float, running: list[str] | None = None) -> dict[str, Any]:
    return {
        "host": platform.node(),
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "helper_uptime_seconds": round(time.time() - STARTED),
        "running_actions": running or [],
        "outbox": Outbox().status(),
        "log": log_since(since),
    }
