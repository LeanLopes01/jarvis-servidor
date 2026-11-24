"""Gemini model quota in Antigravity, Google's agent IDE, on this PC.

Antigravity keeps no quota file. Two sources, best first:

- live: while Antigravity is open, its local language server answers the same
  status request its own quota panel uses (an internal, undocumented interface:
  it may change with an update). Only 127.0.0.1 is contacted, with the CSRF token
  the IDE passes to that process; nothing is stored.
- log: when a model's quota runs out, the language server logs "Your quota will
  reset after 35h31m1s". That gives "limit reached" and the reset time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
_RESET = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ .*?quota will reset after "
    r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?"
)


def logs_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "Antigravity" / "logs"


def limit_from_logs(now: float, root: Path | None = None) -> dict[str, Any] | None:
    """The latest "quota exhausted" in the logs, while its window is still open."""
    latest: float | None = None
    for path in (root or logs_dir()).rglob("*.log"):
        try:
            if path.stat().st_mtime < now - 8 * 86400:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _RESET.match(line)
            if not match or not any(match.groups()[1:]):
                continue
            logged = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").timestamp()
            hours, minutes, seconds = (int(g or 0) for g in match.groups()[1:])
            resets = logged + hours * 3600 + minutes * 60 + seconds
            latest = max(latest or 0, resets)
    if latest is None or latest <= now:
        return None
    return {
        "resets_at": datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M"),
        "resets_in_minutes": int((latest - now) // 60),
    }


def _language_servers() -> list[tuple[str, list[int]]]:
    """(csrf token, listening ports) of each running Antigravity language server."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info["name"] or "").lower()
            args = process.info["cmdline"] or []
            if "language_server" not in name or "--csrf_token" not in args:
                continue
            token = args[args.index("--csrf_token") + 1]
            ports = sorted(
                {
                    c.laddr.port
                    for c in process.net_connections(kind="tcp")
                    if c.status == psutil.CONN_LISTEN
                    and c.laddr.ip in {"127.0.0.1", "::1", "0.0.0.0"}
                }
            )
        except (psutil.Error, IndexError, OSError):
            continue
        if ports:
            found.append((token, ports))
    return found


def _status(token: str, port: int) -> dict[str, Any] | None:
    body = json.dumps(
        {
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "locale": "en",
            }
        }
    ).encode()
    # The server's certificate is self-signed for localhost; only loopback is used.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for scheme in ("https", "http"):
        request = urllib.request.Request(
            f"{scheme}://127.0.0.1:{port}{STATUS_PATH}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "X-Codeium-Csrf-Token": token,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=3, context=context if scheme == "https" else None
            ) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            continue
    return None


def parse_status(status: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """Models with a quota, wherever the response nests them."""
    models: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            quota = node.get("quotaInfo")
            if (
                isinstance(quota, dict)
                and node.get("label")
                # Neither a fraction nor a reset: a model the plan does not offer.
                and (
                    quota.get("remainingFraction") is not None or quota.get("resetTime")
                )
            ):
                model: dict[str, Any] = {
                    "label": str(node["label"])[:60],
                    # Antigravity omits the fraction once a model is used up.
                    "remaining_percent": round(
                        float(quota.get("remainingFraction") or 0) * 100, 1
                    ),
                }
                reset = quota.get("resetTime")
                if isinstance(reset, str):
                    with contextlib.suppress(ValueError):
                        model["reset_epoch"] = datetime.fromisoformat(
                            reset.replace("Z", "+00:00")
                        ).timestamp()
                models.append(model)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(status)
    unique = {m["label"]: m for m in models}
    return list(unique.values())


def cache_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "JarvisDesktop" / "antigravity-usage.json"


def _present(models: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    shown = []
    for model in models:
        view = {k: v for k, v in model.items() if k != "reset_epoch"}
        reset = model.get("reset_epoch")
        if reset:
            if reset <= now:
                # The window reset after this reading: the quota is full again.
                view.update(
                    remaining_percent=100.0, note="reset since the last reading"
                )
            else:
                view["resets_at"] = datetime.fromtimestamp(reset).strftime(
                    "%Y-%m-%d %H:%M"
                )
                view["resets_in_minutes"] = int((reset - now) // 60)
        shown.append(view)
    return shown


def antigravity_usage(
    now: float,
    *,
    live: bool = True,
    logs: Path | None = None,
    cache: Path | None = None,
) -> dict[str, Any] | None:
    cache = cache or cache_file()
    if live:
        for token, ports in _language_servers():
            for port in ports:
                status = _status(token, port)
                models = parse_status(status, now) if status else []
                if models:
                    try:
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        cache.write_text(
                            json.dumps({"observed_at": now, "models": models})
                        )
                    except OSError:
                        pass
                    return {
                        "models": _present(models, now),
                        "source": "live",
                        "observed_minutes_ago": 0,
                    }
    result: dict[str, Any] = {}
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(saved["observed_at"]) < 8 * 86400:
            result = {
                "models": _present(saved["models"], now),
                "source": "last reading",
                "observed_minutes_ago": int((now - float(saved["observed_at"])) // 60),
            }
    except (OSError, ValueError, KeyError, TypeError):
        pass
    hit = limit_from_logs(now, logs)
    if hit:
        result["limit_reached"] = hit
        result.setdefault("source", "log")
    return result or None
