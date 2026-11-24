"""Subscription usage of Claude Code and Codex, and the Claude status line."""

import json
from pathlib import Path

import pytest

from src.jarvis_desktop import claude_status, hooks_setup
from src.jarvis_desktop.usage import usage_report
from tests.test_coding import NOW, iso, write


def codex_session(tmp_path: Path) -> Path:
    limits = {
        "primary": {
            "used_percent": 57.0,
            "window_minutes": 300,
            "resets_at": NOW + 3600,
        },
        "secondary": {
            "used_percent": 25.0,
            "window_minutes": 10080,
            "resets_at": NOW + 86400,
        },
        "plan_type": "plus",
        "rate_limit_reached_type": None,
    }
    records = [
        {
            "timestamp": iso(-60),
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": limits},
        }
    ]
    return write(
        tmp_path / "codex" / "sessions" / "2026" / "10" / "16" / "rollout-a.jsonl",
        records,
    )


def test_codex_and_claude_usage(tmp_path: Path) -> None:
    codex_session(tmp_path)
    saved = tmp_path / "claude-usage.json"
    claude_status.save(
        {
            "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 7200},
            "seven_day": {"used_percentage": 41.2, "resets_at": NOW - 10},
        },  # weekly already reset
        saved,
    )
    data = json.loads(saved.read_text())
    data["observed_at"] = NOW - 120
    saved.write_text(json.dumps(data))

    report = usage_report(
        NOW,
        live=False,
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        claude_saved=saved,
    )
    codex = report["codex"]
    assert codex["plan"] == "plus"
    assert codex["five_hour"]["used_percent"] == 57.0
    assert codex["five_hour"]["resets_in_minutes"] == 60
    assert codex["weekly"]["used_percent"] == 25.0
    claude = report["claude_code"]
    assert claude["five_hour"]["used_percent"] == 23.5
    assert claude["weekly"]["used_percent"] == 0  # reset since it was recorded
    assert claude["observed_minutes_ago"] == 2


def test_claude_limit_hit_from_session_log(tmp_path: Path) -> None:
    record = {
        "type": "assistant",
        "timestamp": iso(-30),
        "apiErrorParams": {
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": NOW + 1800,
                "rateLimitType": "five_hour",
            }
        },
    }
    write(tmp_path / "claude" / "projects" / "c--x" / "s.jsonl", [record])
    report = usage_report(
        NOW,
        live=False,
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "none",
        claude_saved=tmp_path / "missing.json",
    )
    assert report["claude_code"]["limit_reached"]["type"] == "five_hour"
    assert report["claude_code"]["limit_reached"]["resets_in_minutes"] == 30
    assert report["codex"] is None and "codex_note" in report


def test_status_line_text() -> None:
    data = {
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 23.4},
            "seven_day": {"used_percentage": 41.6},
        },
        "context_window": {"used_percentage": 12},
    }
    assert claude_status.line(data) == "Opus · 5h 23% · 7d 42% · ctx 12%"
    assert claude_status.line({}) == "Claude"


def test_status_line_install_respects_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}")
    hooks_setup.install()
    assert (
        "claude_status.py" in json.loads(settings.read_text())["statusLine"]["command"]
    )
    hooks_setup.uninstall()
    assert "statusLine" not in json.loads(settings.read_text())

    mine = {"type": "command", "command": "my-status.sh"}
    settings.write_text(json.dumps({"statusLine": mine}))
    hooks_setup.install()
    hooks_setup.uninstall()
    assert json.loads(settings.read_text())["statusLine"] == mine


def test_claude_usage_from_the_usage_panel_cache(tmp_path: Path) -> None:
    state = tmp_path / ".claude.json"
    state.write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "not read"},
                "cachedUsageUtilization": {
                    "fetchedAtMs": (NOW - 300) * 1000,
                    "utilization": {
                        "five_hour": {"utilization": 12, "resets_at": None},
                        "seven_day": {
                            "utilization": 44,
                            "resets_at": iso(2 * 86400).replace("Z", "+00:00"),
                        },
                    },
                },
            }
        )
    )
    report = usage_report(
        NOW,
        live=False,
        claude_root=tmp_path,
        codex_root=tmp_path,
        claude_saved=tmp_path / "x.json",
        claude_state=state,
    )
    claude = report["claude_code"]
    assert claude["five_hour"] == {"used_percent": 12.0}
    assert claude["weekly"]["used_percent"] == 44.0
    assert claude["weekly"]["resets_in_minutes"] == 2 * 24 * 60
    assert claude["observed_minutes_ago"] == 5
    assert "not read" not in json.dumps(report)


def _credentials(tmp_path, expires_at_ms):
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_claude_live_usage_reads_the_usage_endpoint(tmp_path, monkeypatch):
    import io
    import urllib.request

    from src.jarvis_desktop import usage

    now = 1_800_000_000.0
    seen = {}

    def fake_urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        body = {
            "five_hour": {
                "utilization": 61.0,
                "resets_at": "2027-01-15T10:00:00+00:00",
            },
            "seven_day": {"utilization": 9.0, "resets_at": "2027-01-20T10:00:00+00:00"},
        }
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = usage.claude_live_usage(now, _credentials(tmp_path, (now + 3600) * 1000))
    assert seen["auth"] == "Bearer tok"
    assert result["source"] == "live" and result["plan"] == "max"
    assert result["five_hour"]["used_percent"] == 61.0
    assert result["weekly"]["used_percent"] == 9.0


def test_claude_live_usage_never_uses_an_expired_token(tmp_path, monkeypatch):
    import urllib.request

    from src.jarvis_desktop import usage

    def no_network(*args, **kwargs):
        raise AssertionError("expired token must not be sent")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    now = 1_800_000_000.0
    assert (
        usage.claude_live_usage(now, _credentials(tmp_path, (now - 10) * 1000)) is None
    )
    assert usage.claude_live_usage(now, tmp_path / "missing.json") is None
