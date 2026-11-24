"""Claude Code status line that also records the subscription usage for JARVIS.

Claude Code gives the status line command its session JSON on stdin, including
rate_limits.five_hour / seven_day (used_percentage, resets_at) for Pro and Max
plans. This script saves that part to %LOCALAPPDATA%/JarvisDesktop/claude-usage.json
and prints a short line. Standard library only; it never fails loudly.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def usage_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "JarvisDesktop" / "claude-usage.json"


def save(limits: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rate_limits": limits, "observed_at": time.time()}
    with tempfile.NamedTemporaryFile(
        "w", dir=target.parent, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(payload, tmp)
    os.replace(tmp.name, target)


def line(data: dict) -> str:
    parts = [str((data.get("model") or {}).get("display_name") or "Claude")]
    limits = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = limits.get(key) or {}
        if window.get("used_percentage") is not None:
            parts.append(f"{label} {round(window['used_percentage'])}%")
    context = (data.get("context_window") or {}).get("used_percentage")
    if context is not None:
        parts.append(f"ctx {round(context)}%")
    return " · ".join(parts)


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        if isinstance(data.get("rate_limits"), dict) and data["rate_limits"]:
            save(data["rate_limits"], usage_file())
        print(line(data))
    except Exception:  # the status line must never break Claude Code
        print("Claude")


if __name__ == "__main__":
    main()
