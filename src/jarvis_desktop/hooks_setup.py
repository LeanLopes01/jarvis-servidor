"""Installs the agent_hook.py hooks into Claude Code and Codex user settings.

Existing hooks are kept: ours are recognised by the script path, replaced on reinstall
and removed on uninstall. A timestamped backup is written before every change.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .coding import antigravity_config_dir, claude_dir, codex_dir

SCRIPT = Path(__file__).with_name("agent_hook.py")
STATUS_SCRIPT = Path(__file__).with_name("claude_status.py")
MARKER = "agent_hook.py"
CLAUDE_EVENTS = ["UserPromptSubmit", "PermissionRequest", "Notification", "Stop"]
CODEX_EVENTS = ["UserPromptSubmit", "PermissionRequest", "Stop"]
ANTIGRAVITY_HOOK = "jarvis-agent-hook"
ANTIGRAVITY_EVENTS = ["PreInvocation", "Stop"]


def _python() -> Path:
    # pythonw.exe (used for the background helper) gives hooks no stdin on some setups.
    return Path(sys.executable).with_name(
        "python.exe" if sys.platform == "win32" else "python"
    )


def _quote(path: Path) -> str:
    text = path.as_posix()
    return f'"{text}"' if " " in text else text


def _is_ours(group: dict[str, Any]) -> bool:
    return MARKER in json.dumps(group)


def _merge(
    config: dict[str, Any], events: list[str], group: dict[str, Any] | None
) -> None:
    hooks = config.setdefault("hooks", {})
    for event in list(hooks):
        groups = [g for g in hooks[event] if not _is_ours(g)]
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if group is not None:
        for event in events:
            hooks.setdefault(event, []).append(group)
    if not hooks:
        del config["hooks"]


def _rewrite(path: Path, update: Any) -> Path | None:
    config: dict[str, Any] = {}
    backup = None
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
        backup = path.with_name(
            f"{path.name}.jarvis-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(path, backup)
    update(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return backup


def claude_settings() -> Path:
    return claude_dir() / "settings.json"


def codex_hooks_file() -> Path:
    return codex_dir() / "hooks.json"


def antigravity_hooks_file() -> Path:
    return antigravity_config_dir() / "hooks.json"


def claude_group() -> dict[str, Any]:
    # Exec form (no shell); async so the hook can never delay or decide anything.
    return {
        "hooks": [
            {
                "type": "command",
                "command": _python().as_posix(),
                "args": [SCRIPT.as_posix(), "claude"],
                "async": True,
            }
        ]
    }


def codex_group() -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{_quote(_python())} {_quote(SCRIPT)} codex",
                "timeout": 10,
                "async": True,
            }
        ]
    }


def antigravity_entry() -> dict[str, Any]:
    return {
        event: [
            {
                "type": "command",
                "command": f"{_quote(_python())} {_quote(SCRIPT)} antigravity {event}",
                "timeout": 10,
            }
        ]
        for event in ANTIGRAVITY_EVENTS
    }


def _merge_antigravity(config: dict[str, Any], entry: dict[str, Any] | None) -> None:
    for key in list(config):
        if key == ANTIGRAVITY_HOOK or MARKER in json.dumps(config[key]):
            del config[key]
    if entry is not None:
        config[ANTIGRAVITY_HOOK] = entry


def _set_status_line(config: dict[str, Any], install: bool) -> None:
    """Add our status line (it records Claude's usage limits) unless one exists."""
    current = config.get("statusLine")
    ours = isinstance(current, dict) and STATUS_SCRIPT.name in json.dumps(current)
    if install and (current is None or ours):
        config["statusLine"] = {
            "type": "command",
            "command": f"{_quote(_python())} {_quote(STATUS_SCRIPT)}",
        }
    elif not install and ours:
        del config["statusLine"]


def install() -> list[str]:
    notes = []

    def update_claude(config: dict[str, Any]) -> None:
        _merge(config, CLAUDE_EVENTS, claude_group())
        _set_status_line(config, install=True)

    backup = _rewrite(claude_settings(), update_claude)
    notes.append(
        f"Claude Code: {claude_settings()} (backup: {backup or 'none, new file'})"
    )
    backup = _rewrite(
        codex_hooks_file(), lambda c: _merge(c, CODEX_EVENTS, codex_group())
    )
    notes.append(f"Codex: {codex_hooks_file()} (backup: {backup or 'none, new file'})")
    backup = _rewrite(
        antigravity_hooks_file(),
        lambda c: _merge_antigravity(c, antigravity_entry()),
    )
    notes.append(
        f"Antigravity: {antigravity_hooks_file()} (backup: {backup or 'none, new file'})"
    )
    return notes


def uninstall() -> list[str]:
    notes = []
    for path, events in (
        (claude_settings(), CLAUDE_EVENTS),
        (codex_hooks_file(), CODEX_EVENTS),
    ):
        if path.exists():

            def remove(config: dict[str, Any], e: list[str] = events) -> None:
                _merge(config, e, None)
                _set_status_line(config, install=False)

            backup = _rewrite(path, remove)
            notes.append(f"removed from {path} (backup: {backup})")
    agy_path = antigravity_hooks_file()
    if agy_path.exists():
        backup = _rewrite(agy_path, lambda c: _merge_antigravity(c, None))
        notes.append(f"removed from {agy_path} (backup: {backup})")
    return notes
