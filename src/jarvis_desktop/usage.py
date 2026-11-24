"""Subscription usage of the coding agents on this PC: how much of the 5-hour and
weekly limits is used, and when each window resets.

Codex writes its rate limits into every session log (token_count events). Claude Code
is read live from its usage endpoint with the local sign-in; otherwise from its status
line (claude_status.py saves them to a file) or its usage panel cache. When a limit is
hit, the session log also records when it resets.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .antigravity import antigravity_usage
from .claude_status import usage_file
from .codex_live import read_rate_limits
from .coding import _read_records, _recent, claude_dir, codex_dir, last_activity

STALE_HOURS = 6


def _window(
    used: float | None, resets_at: float | None, now: float
) -> dict[str, Any] | None:
    if used is None and resets_at is None:
        return None
    window: dict[str, Any] = {}
    if used is not None:
        window["used_percent"] = round(float(used), 1)
    if resets_at:
        if resets_at <= now:
            # The window reset after the last recorded use, so nothing counts yet.
            return {"used_percent": 0, "note": "reset since the last recorded use"}
        window["resets_at"] = datetime.fromtimestamp(resets_at).strftime(
            "%Y-%m-%d %H:%M"
        )
        window["resets_in_minutes"] = int((resets_at - now) // 60)
    return window


def codex_live_usage(now: float) -> dict[str, Any] | None:
    """Fresh figures from Codex's app-server; None when it cannot be reached."""
    try:
        data = read_rate_limits()
    except OSError:
        return None
    limits = (data or {}).get("rateLimits")
    if not isinstance(limits, dict):
        return None
    primary = limits.get("primary") or {}
    secondary = limits.get("secondary") or {}
    return {
        "plan": limits.get("planType"),
        "five_hour": _window(primary.get("usedPercent"), primary.get("resetsAt"), now),
        "weekly": _window(secondary.get("usedPercent"), secondary.get("resetsAt"), now),
        "limit_reached": limits.get("rateLimitReachedType"),
        "observed_minutes_ago": 0,
        "source": "live",
    }


def codex_usage(now: float, root: Path | None = None) -> dict[str, Any] | None:
    files = _recent(
        list(((root or codex_dir()) / "sessions").glob("*/*/*/*.jsonl")),
        now - 14 * 86400,
    )
    for path in files[:5]:
        for record in reversed(_read_records(path)):
            payload = (
                record.get("payload") if isinstance(record.get("payload"), dict) else {}
            )
            limits = payload.get("rate_limits")
            if payload.get("type") != "token_count" or not isinstance(limits, dict):
                continue
            primary = limits.get("primary") or {}
            secondary = limits.get("secondary") or {}
            observed = last_activity(path)
            return {
                "plan": limits.get("plan_type"),
                "five_hour": _window(
                    primary.get("used_percent"), primary.get("resets_at"), now
                ),
                "weekly": _window(
                    secondary.get("used_percent"), secondary.get("resets_at"), now
                ),
                "limit_reached": limits.get("rate_limit_reached_type"),
                "observed_minutes_ago": int((now - observed) // 60),
            }
    return None


def _claude_limit_hit(now: float, root: Path | None) -> dict[str, Any] | None:
    files = _recent(
        list(((root or claude_dir()) / "projects").glob("*/*.jsonl")), now - 7 * 86400
    )
    for path in files[:5]:
        for record in reversed(_read_records(path)):
            info = (record.get("apiErrorParams") or {}).get("rate_limit_info")
            if isinstance(info, dict) and (info.get("resetsAt") or 0) > now:
                return {
                    "type": info.get("rateLimitType"),
                    "resets_at": datetime.fromtimestamp(info["resetsAt"]).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "resets_in_minutes": int((info["resetsAt"] - now) // 60),
                }
    return None


def claude_state_file() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (
        Path(configured) / ".claude.json"
        if configured
        else Path.home() / ".claude.json"
    )


def _iso_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _cached_utilization(state: Path) -> dict[str, Any]:
    """The usage panel cache Claude Code keeps (also in the VS Code extension).

    Only the cachedUsageUtilization block is read; nothing else from that file.
    """
    try:
        cache = json.loads(state.read_text(encoding="utf-8")).get(
            "cachedUsageUtilization"
        )
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(cache, dict):
        return {}
    usage = cache.get("utilization") or {}
    limits = {}
    for key in ("five_hour", "seven_day"):
        window = usage.get(key) or {}
        if window.get("utilization") is not None or window.get("resets_at"):
            limits[key] = {
                "used_percentage": window.get("utilization"),
                "resets_at": _iso_seconds(window.get("resets_at")),
            }
    return {
        "rate_limits": limits,
        "observed_at": (cache.get("fetchedAtMs") or 0) / 1000,
    }


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_LIVE_CACHE: tuple[float, dict[str, Any]] | None = None
_CLAUDE_LIVE_TTL_SECONDS = 60.0


def _save_claude_fallback(
    now: float, five: dict[str, Any], week: dict[str, Any]
) -> None:
    """Persist the latest live Claude limits so 429s or offline reads keep showing them."""
    limits: dict[str, Any] = {}
    if five.get("utilization") is not None or five.get("resets_at"):
        limits["five_hour"] = {
            "used_percentage": five.get("utilization"),
            "resets_at": _iso_seconds(five.get("resets_at")),
        }
    if week.get("utilization") is not None or week.get("resets_at"):
        limits["seven_day"] = {
            "used_percentage": week.get("utilization"),
            "resets_at": _iso_seconds(week.get("resets_at")),
        }
    if not limits:
        return
    try:
        target = usage_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"observed_at": now, "rate_limits": limits}),
            encoding="utf-8",
        )
    except OSError:
        pass


def claude_credentials_file() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(configured) if configured else Path.home() / ".claude") / (
        ".credentials.json"
    )


def claude_live_usage(
    now: float, credentials: Path | None = None
) -> dict[str, Any] | None:
    """Fresh figures from the usage endpoint Claude Code's /usage panel reads, with
    the subscription sign-in Claude Code keeps on this PC. The status line only runs
    in the terminal, so sessions in the VS Code extension never refresh that file.
    None when signed out, the token expired (Claude Code renews it on its next use;
    this never does) or the request fails: the saved figures are used instead."""
    global _CLAUDE_LIVE_CACHE
    import urllib.error
    import urllib.request

    if (
        credentials is None
        and _CLAUDE_LIVE_CACHE is not None
        and now - _CLAUDE_LIVE_CACHE[0] < _CLAUDE_LIVE_TTL_SECONDS
    ):
        return dict(_CLAUDE_LIVE_CACHE[1])

    try:
        oauth = (
            json.loads(
                (credentials or claude_credentials_file()).read_text(encoding="utf-8")
            ).get("claudeAiOauth")
            or {}
        )
    except (OSError, ValueError, AttributeError):
        return None
    token = oauth.get("accessToken")
    if not token or (oauth.get("expiresAt") or 0) / 1000 <= now:
        return None
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "jarvis-desktop",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    five = body.get("five_hour") or {}
    week = body.get("seven_day") or {}
    if five.get("utilization") is None and week.get("utilization") is None:
        return None
    locked = five.get("locked_reason") or week.get("locked_reason")
    result = {
        "plan": oauth.get("subscriptionType"),
        "five_hour": _window(
            five.get("utilization"), _iso_seconds(five.get("resets_at")), now
        ),
        "weekly": _window(
            week.get("utilization"), _iso_seconds(week.get("resets_at")), now
        ),
        "limit_reached": locked,
        "observed_minutes_ago": 0,
        "source": "live",
    }
    if credentials is None:
        _CLAUDE_LIVE_CACHE = (now, dict(result))
        _save_claude_fallback(now, five, week)
    return result


def claude_usage(
    now: float,
    root: Path | None = None,
    saved: Path | None = None,
    state: Path | None = None,
) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    try:
        from_status_line = json.loads(
            (saved or usage_file()).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        from_status_line = {}
    from_cache = _cached_utilization(state or claude_state_file())
    data = max((from_status_line, from_cache), key=lambda d: d.get("observed_at") or 0)
    limits = data.get("rate_limits") or {}
    observed = data.get("observed_at") or 0
    if limits and now - observed < 7 * 86400:
        five = limits.get("five_hour") or {}
        week = limits.get("seven_day") or {}
        result["five_hour"] = _window(
            five.get("used_percentage"), five.get("resets_at"), now
        )
        result["weekly"] = _window(
            week.get("used_percentage"), week.get("resets_at"), now
        )
        result["observed_minutes_ago"] = int((now - observed) // 60)
        if now - observed > STALE_HOURS * 3600:
            result["note"] = (
                "These figures are from the last Claude Code session and may be out of date."
            )
    hit = _claude_limit_hit(now, root)
    if hit:
        result["limit_reached"] = hit
    if not result:
        return None
    return result


def usage_report(
    now: float | None = None,
    *,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    claude_saved: Path | None = None,
    claude_state: Path | None = None,
    antigravity_logs: Path | None = None,
    live: bool = True,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    report: dict[str, Any] = {
        "claude_code": (claude_live_usage(now) if live else None)
        or claude_usage(now, claude_root, claude_saved, claude_state),
        "codex": (codex_live_usage(now) if live else None)
        or codex_usage(now, codex_root),
        "antigravity": antigravity_usage(now, live=live, logs=antigravity_logs),
    }
    if report["claude_code"] is None:
        report["claude_code_note"] = (
            "No usage figures yet: they arrive after the next Claude Code response."
        )
    if report["codex"] is None:
        report["codex_note"] = "No Codex usage recorded in the last two weeks."
    if report["antigravity"] is None:
        report["antigravity_note"] = (
            "Gemini quota in Antigravity is read live only while Antigravity is open;"
            " no limit was hit recently."
        )
    return report
