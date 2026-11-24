"""Lets the headless Antigravity coordinator read the project with read-only commands.

In print mode Antigravity cannot ask for permission, and a refused command ends its
turn without an answer. It searches files with PowerShell, so one allow rule is
added to ~/.gemini/antigravity-cli/settings.json: a single pipeline of reading
commands (list, read, search, select, sort, count). Outside quotes the rule refuses
`;`, `&`, `|` (except between the allowed commands), `$`, backticks, parentheses,
braces, redirections and line breaks, so nothing can be written or executed
through it. Quoted arguments may hold those characters, because PowerShell takes
them literally there (`-Pattern "A|B"` is a regex, not a pipe): single quotes
expand nothing; double quotes still expand `$` and backtick escapes, so those stay
refused inside them. Unicode quotes (U+2018 to U+201E) also delimit strings in PowerShell
and are refused everywhere, so they cannot disguise where a string ends. Every
other command still needs permission (and is refused in print mode). The rule also
applies to the user's own Antigravity CLI: those reading commands no longer ask for
confirmation there either.

Existing settings are kept; a timestamped backup is written before each change.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

_UNICODE_QUOTES = "\u2018\u2019\u201a\u201b\u201c\u201d\u201e"  # curly quotes
_ARG_PIECE = (
    r"[^;&|`$(){}<>\"'\r\n" + _UNICODE_QUOTES + "]"  # unquoted text
    r"|\$(true|false|null)"
    r"|\"[^\"`$\r\n" + _UNICODE_QUOTES + ']*"'  # "A|B": literal except $ and `
    r"|'[^'\r\n" + _UNICODE_QUOTES + "]*'"  # 'A|B': fully literal
)
_ARGS = "( (" + _ARG_PIECE + ")*)?"
READ_ONLY_RULE = (
    "command(regex:^(Get-ChildItem|gci|ls|dir|Get-Content|gc|cat|type|Select-String|sls|"
    "Get-Item|Test-Path|Resolve-Path|rg|findstr)"
    + _ARGS
    + r"( \| (Select-Object|select|Sort-Object|sort|Measure-Object|measure|Select-String|sls|"
    "Format-List|Format-Table|Out-String)" + _ARGS + ")*$)"
)
RAG_MCP_RULE = "mcp(dicevtt-rag/*)"


def settings_file() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def _rewrite(update: Any) -> Path | None:
    path = settings_file()
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


def _ours(rule: str) -> bool:
    return rule.startswith("command(regex:^(Get-ChildItem|gci|") or rule == RAG_MCP_RULE


def install() -> str:
    def update(config: dict[str, Any]) -> None:
        permissions = config.setdefault("permissions", {})
        allow = [r for r in permissions.get("allow", []) if not _ours(r)]
        permissions["allow"] = [*allow, READ_ONLY_RULE, RAG_MCP_RULE]

    backup = _rewrite(update)
    return f"Antigravity: read-only command and dicevtt-rag MCP rules in {settings_file()} (backup: {backup or 'none, new file'})"


def uninstall() -> str:
    if not settings_file().exists():
        return "no Antigravity settings file"

    def update(config: dict[str, Any]) -> None:
        permissions = config.get("permissions") or {}
        permissions["allow"] = [r for r in permissions.get("allow", []) if not _ours(r)]
        if not permissions["allow"]:
            permissions.pop("allow")
        if not permissions:
            config.pop("permissions", None)

    backup = _rewrite(update)
    return f"removed the rule from {settings_file()} (backup: {backup})"
