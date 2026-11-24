"""Live Codex rate limits from Codex's own app-server (JSON-RPC over stdio).

`account/rateLimits/read` returns the same figures the Codex extension shows, fresh
from OpenAI, using Codex's existing login. Nothing is sent to the model.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

TIMEOUT = 20.0


def codex_executable() -> Path | None:
    """The newest Codex binary bundled with the VS Code extension."""
    found = sorted(
        (Path.home() / ".vscode" / "extensions").glob(
            "openai.chatgpt-*/bin/windows-x86_64/codex.exe"
        )
    )
    return found[-1] if found else None


def read_rate_limits(
    executable: Path | None = None, timeout: float = TIMEOUT
) -> dict[str, Any] | None:
    executable = executable or codex_executable()
    if executable is None:
        return None
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [str(executable), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    result: dict[str, Any] = {}
    done = threading.Event()

    def reader() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") == 2:
                result.update(message.get("result") or {})
                done.set()
                return

    threading.Thread(target=reader, daemon=True).start()
    try:
        assert process.stdin is not None
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "jarvis", "version": "1.0"}},
            },
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
        ):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
        done.wait(timeout)
    finally:
        process.kill()
    return result or None
