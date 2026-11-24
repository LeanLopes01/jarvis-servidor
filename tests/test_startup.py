"""Check both entrypoint styles without connecting to model providers."""

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "arguments",
    [
        ["src/agent.py", "--help"],
        [
            "-c",
            "from pathlib import Path; "
            "from livekit.agents.cli.discover import get_import_data; "
            "app = get_import_data(path=Path('src/agent.py')); "
            "assert app.import_string == 'src.agent:server'",
        ],
    ],
)
def test_agent_entrypoint_imports(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
