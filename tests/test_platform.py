"""Checks for behavior that differs between the Windows PC and the Linux server."""

import pytest

from src import browser as browser_module
from src import spotify_cli as spotify_module
from src.browser import default_headless
from src.spotify import SpotifyTools


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)],
)
def test_headless_env_override(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("JARVIS_BROWSER_HEADLESS", value)
    assert default_headless() is expected


@pytest.mark.parametrize(
    ("platform", "display", "expected"),
    [
        ("win32", None, False),
        ("darwin", None, False),
        ("linux", None, True),
        ("linux", ":0", False),
    ],
)
def test_headless_default_follows_platform_and_display(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    display: str | None,
    expected: bool,
) -> None:
    monkeypatch.delenv("JARVIS_BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display is None:
        monkeypatch.delenv("DISPLAY", raising=False)
    else:
        monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setattr(browser_module.sys, "platform", platform)

    assert default_headless() is expected


def test_spotify_tools_are_left_out_without_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spotify_module, "_find_cli", lambda: None)
    tools = SpotifyTools()

    assert not tools.available
    assert tools.tools == []


def test_agent_does_not_mention_spotify_tools_without_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import Assistant

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(spotify_module, "_find_cli", lambda: None)
    assistant = Assistant()

    assert "play_on_spotify" not in assistant.instructions
    assert "{spotify_instructions}" not in assistant.instructions
    assert "Spotify on the user's computer is not reachable" in assistant.instructions
    assert not any("spotify" in tool.id for tool in assistant.tools)


def test_ready_file_is_written_for_jarvis_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    from src.agent import _write_ready_file

    ready = tmp_path / "ready"
    monkeypatch.setenv("JARVIS_READY_FILE", str(ready))
    _write_ready_file("AW_test", object())
    assert json.loads(ready.read_text())["worker_id"] == "AW_test"

    monkeypatch.delenv("JARVIS_READY_FILE")
    _write_ready_file("AW_other")  # no file configured: nothing happens
    assert json.loads(ready.read_text())["worker_id"] == "AW_test"
