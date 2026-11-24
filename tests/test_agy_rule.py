"""The read-only command rule for the headless Antigravity coordinator.

Allowed: one pipeline of reading commands, with literal text in quotes (a regex
alternation like "A|B" is not a pipe). Refused: anything that could write or run
another command, including tricks with quotes and line breaks.
"""

from __future__ import annotations

import re

import pytest

from src.jarvis_desktop.agy_setup import READ_ONLY_RULE

PATTERN = re.compile(READ_ONLY_RULE.removeprefix("command(regex:").removesuffix(")"))

ALLOWED = [
    # The command refused on 22/10 (job 9d42ceef): alternation inside double quotes.
    "Get-ChildItem -Path Assets -Recurse -Filter *.cs | Select-String -Pattern "
    '"OpenFileDialog|FileBrowser|LoadImage|persistentDataPath" | Select-Object '
    "-ExpandProperty Path -Unique",
    # The first attempt of the same job, also refused for the quoted "|".
    "Get-Content Assets\\Scripts\\Salas\\BarraFerramentasManager.cs | Select-String "
    '-Pattern "Dados3D|GerenciadorDados3D"',
    "Select-String -Path Assets/*.cs -Pattern 'Dado(3D)?|Skin\\w+' | Select-Object -First 20",
    'Get-Content "Assets/Scripts/Dados/Dado 3D.cs"',
    "Get-ChildItem -Recurse -Filter *.cs | Measure-Object",
    'rg -n "class \\w+Skin" Assets',
    "gci Assets -Recurse -Force:$false | sort Name",
    "Get-Content 'It''s.txt'",
    'Select-String -Pattern "a;b&c(d){e}<f>" -Path x.cs',
]

REFUSED = [
    "Get-Content a.cs; Remove-Item a.cs",
    "Get-Content a.cs | Remove-Item",
    'Get-Content "$(Remove-Item x)"',
    'Get-Content "$env:USERPROFILE/.ssh/id_rsa"',
    'Get-Content "a`"; Remove-Item x; `""',
    "Get-Content a.cs > copy.cs",
    "Get-Content a.cs\nRemove-Item a.cs",
    "Get-Content a.cs\r\nRemove-Item a.cs",
    # Unicode quotes end strings in PowerShell: they must not hide a command.
    'Get-Content \u2018"\u2019 ; Remove-Item x \u2018"\u2019',
    "Get-Content \u201ca\u201d; Remove-Item x",
    "Get-Content 'unterminated",
    "Get-Content (Remove-Item x)",
    "Get-Content & Remove-Item x",
    "Set-Content a.cs 'x'",
    "Invoke-Expression 'Remove-Item x'",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_reading_commands_pass(command: str) -> None:
    assert PATTERN.fullmatch(command), command


@pytest.mark.parametrize("command", REFUSED)
def test_anything_that_writes_or_runs_more_is_refused(command: str) -> None:
    assert not PATTERN.fullmatch(command), command


def test_new_rule_replaces_the_old_one_on_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pathlib import Path as _P
    from src.jarvis_desktop import agy_setup
    from src.jarvis_desktop.agy_setup import RAG_MCP_RULE, _ours

    old = "command(regex:^(Get-ChildItem|gci|ls|dir)( ([^;&|`$(){}<>])*)?$)"
    assert _ours(old) and _ours(READ_ONLY_RULE) and _ours(RAG_MCP_RULE)
    target = tmp_path / "settings.json"
    monkeypatch.setattr(agy_setup, "settings_file", lambda: _P(target))
    agy_setup.install()
    import json
    allow = json.loads(target.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert READ_ONLY_RULE in allow and RAG_MCP_RULE in allow
