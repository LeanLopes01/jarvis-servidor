"""SQLite persistence shared by the service and the admin CLI."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIVE_STATES = ("starting", "ready", "active")
DEVICE_KINDS = ("app", "desktop")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    room TEXT NOT NULL,
    state TEXT NOT NULL,
    idempotency_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ready_at REAL,
    last_presence_at REAL,
    last_heartbeat_at REAL,
    ended_at REAL,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions (state);
CREATE TABLE IF NOT EXISTS desktop_actions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    finished_at REAL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_profiles (
    device_id TEXT PRIMARY KEY,
    master INTEGER NOT NULL DEFAULT 0,
    music_target TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    source_device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT,
    agent TEXT,
    host TEXT,
    project TEXT,
    session_id TEXT,
    turn_id TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    report TEXT,
    started_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS event_deliveries (
    event_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    received_at REAL,
    displayed_at REAL,
    announced_at REAL,
    read_at REAL,
    PRIMARY KEY (event_id, device_id)
);
CREATE TABLE IF NOT EXISTS mobile_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    app TEXT NOT NULL,
    sender TEXT,
    conversation TEXT,
    posted_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (device_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT,
    host TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deadline REAL,
    cancel_requested_at REAL,
    finished_at REAL,
    action_id TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS operations_device ON operations (device_id, created_at);
CREATE TABLE IF NOT EXISTS conversation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,
    text TEXT,
    data TEXT,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversation_items_room ON conversation_items (room, id);
CREATE TABLE IF NOT EXISTS diagnostic_reports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    device_id TEXT,
    state TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    finished_at REAL,
    note TEXT,
    summary TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    result TEXT,
    PRIMARY KEY (device_id, id)
);
"""

DELIVERY_STAGES = ("received", "displayed", "announced", "read")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(char for char in code.upper() if char.isalnum())


@dataclass(frozen=True)
class Device:
    id: str
    kind: str
    name: str
    created_at: float
    last_seen_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class Profile:
    device_id: str
    master: bool
    music_target: str | None
    work_access: bool = False

    def view(self) -> dict[str, Any]:
        return {
            "master": self.master,
            "music_target": self.music_target,
            "work_access": self.master or self.work_access,
        }


@dataclass(frozen=True)
class Session:
    id: str
    device_id: str
    room: str
    state: str
    idempotency_key: str | None
    created_at: float
    updated_at: float
    ready_at: float | None
    last_presence_at: float | None
    last_heartbeat_at: float | None
    ended_at: float | None
    end_reason: str | None
    context: str | None = None  # JSON: routine and phone location for the agent

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES


class Store:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        from .integrations import Integrations

        self.integrations = Integrations(self._db)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS device_work_access (device_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(sessions)")
        }
        if "context" not in columns:
            self._db.execute("ALTER TABLE sessions ADD COLUMN context TEXT")
        code_columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(pairing_codes)")
        }
        if "master" not in code_columns:
            self._db.execute(
                "ALTER TABLE pairing_codes ADD COLUMN master INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # Devices and pairing

    def create_pairing_code(
        self, kind: str, ttl_seconds: float, now: float, *, master: bool = False
    ) -> str:
        if kind not in DEVICE_KINDS:
            raise ValueError(f"kind must be one of {DEVICE_KINDS}")
        if master and kind != "app":
            raise ValueError("only a phone app can be the master device")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self._db.execute(
            "INSERT INTO pairing_codes (code_hash, kind, created_at, expires_at,"
            " used_at, master) VALUES (?, ?, ?, ?, NULL, ?)",
            (token_hash(code), kind, now, now + ttl_seconds, int(master)),
        )
        return f"{code[:4]}-{code[4:]}"

    def redeem_pairing_code(
        self, code: str, name: str, now: float
    ) -> tuple[Device, str] | None:
        """Single-use exchange of a pairing code for a device and its token."""
        code_hash = token_hash(normalize_code(code))
        with self._transaction() as db:
            row = db.execute(
                "SELECT kind, master FROM pairing_codes"
                " WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (code_hash, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE pairing_codes SET used_at = ? WHERE code_hash = ?",
                (now, code_hash),
            )
            token = "jd1." + secrets.token_urlsafe(32)
            device = Device(uuid.uuid4().hex, row["kind"], name[:60], now, None, None)
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (device.id, device.kind, device.name, token_hash(token), now),
            )
            if row["master"]:
                self._set_master(db, device.id, now)
        return device, token

    def device_for_token(self, token: str, now: float) -> Device | None:
        row = self._db.execute(
            "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if row["last_seen_at"] is None or now - row["last_seen_at"] > 60:
            self._db.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, row["id"])
            )
        return _device(row)

    def devices(self) -> list[Device]:
        rows = self._db.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        return [_device(row) for row in rows]

    def revoke_device(self, device_id: str, now: float) -> bool:
        cursor = self._db.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, device_id),
        )
        return cursor.rowcount == 1

    def device_active(self, device_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)
        ).fetchone()
        return row is not None

    # Master phone profile

    @staticmethod
    def _set_master(db: sqlite3.Connection, device_id: str, now: float) -> None:
        # One master at a time: it receives the reports and plays music only on
        # the computer.
        db.execute(
            "UPDATE device_profiles SET master = 0, music_target = NULL,"
            " updated_at = ? WHERE master = 1",
            (now,),
        )
        db.execute(
            "INSERT INTO device_profiles VALUES (?, 1, 'computer', ?)"
            " ON CONFLICT(device_id) DO UPDATE SET master = 1,"
            " music_target = 'computer', updated_at = excluded.updated_at",
            (device_id, now),
        )

    def set_master(self, device_id: str, enabled: bool, now: float) -> bool:
        """Make an active phone the master device, or remove the role."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT kind FROM devices WHERE id = ? AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            if row is None or row["kind"] != "app":
                return False
            if enabled:
                self._set_master(db, device_id, now)
            else:
                db.execute(
                    "UPDATE device_profiles SET master = 0, music_target = NULL,"
                    " updated_at = ? WHERE device_id = ?",
                    (now, device_id),
                )
        return True

    def profile(self, device_id: str) -> Profile:
        work = self._db.execute(
            "SELECT enabled FROM device_work_access WHERE device_id = ?", (device_id,)
        ).fetchone()
        work_access = bool(work and work["enabled"])
        row = self._db.execute(
            "SELECT * FROM device_profiles WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return Profile(device_id, False, None, work_access)
        return Profile(device_id, bool(row["master"]), row["music_target"], work_access)

    def set_work_access(self, device_id: str, enabled: bool) -> bool:
        if not self.device_active(device_id):
            return False
        self._db.execute(
            "INSERT INTO device_work_access VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET enabled = excluded.enabled",
            (device_id, int(enabled)),
        )
        return True

    def can_work(self, device_id: str) -> bool:
        profile = self.profile(device_id)
        return profile.master or profile.work_access

    def can_personal(self, device_id: str) -> bool:
        if not self.device_active(device_id):
            return False
        row = self._db.execute(
            "SELECT enabled FROM device_personal_access WHERE device_id=?", (device_id,)
        ).fetchone()
        return bool(row and row["enabled"])

    def set_personal_access(self, device_id: str, enabled: bool) -> bool:
        if not self.device_active(device_id):
            return False
        self._db.execute(
            "INSERT INTO device_personal_access VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET enabled=excluded.enabled",
            (device_id, int(enabled)),
        )
        return True

    def master_music_target(self, device_id: str) -> str | None:
        """The forced music destination for this device, if it is the master."""
        profile = self.profile(device_id)
        return profile.music_target if profile.master else None

    # Sessions

    def create_session(
        self,
        device_id: str,
        idempotency_key: str | None,
        now: float,
        context: dict[str, Any] | None = None,
    ) -> Session:
        session_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO sessions (id, device_id, room, state, idempotency_key,"
            " created_at, updated_at, context) VALUES (?, ?, ?, 'starting', ?, ?, ?, ?)",
            (
                session_id,
                device_id,
                f"jarvis-{session_id[:12]}",
                idempotency_key,
                now,
                now,
                json.dumps(context) if context else None,
            ),
        )
        session = self.session(session_id)
        assert session is not None
        return session

    def session(self, session_id: str) -> Session | None:
        row = self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return Session(**dict(row)) if row else None

    def session_for_key(self, device_id: str, key: str, since: float) -> Session | None:
        row = self._db.execute(
            "SELECT * FROM sessions WHERE device_id = ? AND idempotency_key = ?"
            " AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (device_id, key, since),
        ).fetchone()
        return Session(**dict(row)) if row else None

    def live_sessions(self) -> list[Session]:
        rows = self._db.execute(
            "SELECT * FROM sessions WHERE state IN (?, ?, ?) ORDER BY created_at",
            LIVE_STATES,
        ).fetchall()
        return [Session(**dict(row)) for row in rows]

    def recent_sessions(self, limit: int) -> list[Session]:
        rows = self._db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Session(**dict(row)) for row in rows]

    def update_session(self, session_id: str, now: float, **fields: Any) -> None:
        allowed = {
            "state",
            "ready_at",
            "last_presence_at",
            "last_heartbeat_at",
            "ended_at",
            "end_reason",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"unexpected session fields: {set(fields) - allowed}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._db.execute(
            f"UPDATE sessions SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), now, session_id),
        )

    # Desktop actions

    def add_action(
        self, device_id: str, action: str, now: float, expires_at: float
    ) -> str:
        action_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO desktop_actions VALUES (?, ?, ?, 'sent', ?, ?, NULL, NULL)",
            (action_id, device_id, action, now, expires_at),
        )
        return action_id

    def finish_action(
        self, action_id: str, state: str, now: float, result: Any = None
    ) -> None:
        text = (
            json.dumps(result, ensure_ascii=False)[:2000]
            if result is not None
            else None
        )
        self._db.execute(
            "UPDATE desktop_actions SET state = ?, finished_at = ?, result = ?"
            " WHERE id = ? AND state = 'sent'",
            (state, now, text, action_id),
        )

    # Routines

    def routines(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT data FROM routines ORDER BY id").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def routine(self, routine_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT data FROM routines WHERE id = ?", (routine_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def save_routine(self, routine: dict[str, Any], now: float) -> dict[str, Any]:
        routine = {**routine, "id": routine.get("id") or uuid.uuid4().hex[:12]}
        self._db.execute(
            "INSERT INTO routines VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE"
            " SET data = excluded.data, updated_at = excluded.updated_at",
            (routine["id"], json.dumps(routine, ensure_ascii=False), now),
        )
        return routine

    def delete_routine(self, routine_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        return cursor.rowcount == 1

    def seed_routines(self, defaults: list[dict[str, Any]], now: float) -> None:
        """Create the default routines once, on a fresh database."""
        if self._db.execute("SELECT COUNT(*) FROM routines").fetchone()[0] == 0:
            for routine in defaults:
                self.save_routine(routine, now)

    # Notifications for the phone

    def add_notification(self, kind: str, title: str, body: str, now: float) -> int:
        cursor = self._db.execute(
            "INSERT INTO notifications (kind, title, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (kind, title[:120], body[:400], now),
        )
        return int(cursor.lastrowid or 0)

    def notifications_after(self, after: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM notifications WHERE id > ? ORDER BY id LIMIT ?",
            (after, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # Agent events (reports from the PC) and their delivery to the master phone

    def add_event(
        self, source_device_id: str, event: dict[str, Any], now: float
    ) -> tuple[dict[str, Any], bool]:
        """Store an event once: (row, created). A repeated id returns the stored row.

        The legacy notification for older app versions is written in the same
        transaction, so an acknowledged event is never half-stored.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM events WHERE id = ?", (event["id"],)
            ).fetchone()
            if row is not None:
                return _event(row), False
            db.execute(
                "INSERT INTO events (id, source_device_id, kind, status, agent, host,"
                " project, session_id, turn_id, title, summary, report, started_at,"
                " finished_at, created_at, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["id"],
                    source_device_id,
                    event["kind"],
                    event.get("status"),
                    event.get("agent"),
                    event.get("host"),
                    event.get("project"),
                    event.get("session_id"),
                    event.get("turn_id"),
                    event["title"],
                    event.get("summary"),
                    event.get("report"),
                    event.get("started_at"),
                    event.get("finished_at"),
                    now,
                    json.dumps(event["data"], ensure_ascii=False)
                    if event.get("data")
                    else None,
                ),
            )
            db.execute(
                "INSERT INTO notifications (kind, title, body, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    event["kind"],
                    event["title"][:120],
                    (event.get("summary") or "")[:400],
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM events WHERE id = ?", (event["id"],)
            ).fetchone()
        return _event(row), True

    def events_after(self, seq: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
        ).fetchall()
        return [_event(row) for row in rows]

    def latest_event_seq(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def event(self, event_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return _event(row, report=True) if row else None

    def mark_delivery(
        self, event_id: str, device_id: str, stage: str, now: float
    ) -> bool:
        if stage not in DELIVERY_STAGES:
            raise ValueError(f"stage must be one of {DELIVERY_STAGES}")
        if (
            self._db.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            is None
        ):
            return False
        # Keep the first time of each stage; a later re-read changes nothing.
        self._db.execute(
            "INSERT INTO event_deliveries (event_id, device_id) VALUES (?, ?)"
            " ON CONFLICT DO NOTHING",
            (event_id, device_id),
        )
        self._db.execute(
            f"UPDATE event_deliveries SET {stage}_at = COALESCE({stage}_at, ?)"
            " WHERE event_id = ? AND device_id = ?",
            (now, event_id, device_id),
        )
        return True

    def deliveries(self, event_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM event_deliveries WHERE event_id = ?", (event_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def coverage(self) -> list[dict[str, Any]]:
        """When each agent and host last produced an event. A host missing here has
        no working completion alerts yet."""
        rows = self._db.execute(
            "SELECT agent, host, kind, MAX(created_at) AS last_at, COUNT(*) AS events"
            " FROM events GROUP BY agent, host, kind ORDER BY agent, host, kind"
        ).fetchall()
        return [dict(row) for row in rows]

    # Message notifications seen by the master phone: metadata only, never the text

    def add_mobile_notifications(
        self, device_id: str, items: list[dict[str, Any]], now: float
    ) -> int:
        stored = 0
        for item in items:
            cursor = self._db.execute(
                "INSERT INTO mobile_notifications (device_id, dedupe_key, app, sender,"
                " conversation, posted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (
                    device_id,
                    item["key"],
                    item["app"],
                    item.get("sender"),
                    item.get("conversation"),
                    item.get("posted_at"),
                    now,
                ),
            )
            stored += cursor.rowcount
        return stored

    def mobile_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM mobile_notifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # Commands from the phone: each id runs at most once

    def begin_command(
        self, device_id: str, command_id: str, kind: str, now: float
    ) -> dict[str, Any] | None:
        """Claim a command id: None when it is new, else the earlier attempt."""
        row = self._db.execute(
            "SELECT * FROM commands WHERE device_id = ? AND id = ?",
            (device_id, command_id),
        ).fetchone()
        if row is not None:
            previous = dict(row)
            previous["result"] = (
                json.loads(previous["result"]) if previous["result"] else None
            )
            return previous
        self._db.execute(
            "INSERT INTO commands VALUES (?, ?, ?, 'running', ?, NULL, NULL)",
            (command_id, device_id, kind, now),
        )
        return None

    def finish_command(
        self, device_id: str, command_id: str, state: str, result: Any, now: float
    ) -> None:
        self._db.execute(
            "UPDATE commands SET state = ?, finished_at = ?, result = ?"
            " WHERE device_id = ? AND id = ?",
            (
                state,
                now,
                json.dumps(result, ensure_ascii=False)[:4000],
                device_id,
                command_id,
            ),
        )

    # Operations: what the phone asked for and where it stands

    def save_operation(self, op: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO operations (id, device_id, kind, label, state, stage, host,"
            " created_at, updated_at, deadline, cancel_requested_at, finished_at,"
            " action_id, result) VALUES (:id, :device_id, :kind, :label, :state, :stage,"
            " :host, :created_at, :updated_at, :deadline, :cancel_requested_at,"
            " :finished_at, :action_id, :result) ON CONFLICT(id) DO UPDATE SET"
            " state = excluded.state, stage = excluded.stage, host = excluded.host,"
            " updated_at = excluded.updated_at, deadline = excluded.deadline,"
            " cancel_requested_at = excluded.cancel_requested_at,"
            " finished_at = excluded.finished_at, action_id = excluded.action_id,"
            " result = excluded.result",
            {
                **op,
                "result": json.dumps(op.get("result"), ensure_ascii=False)[:4000]
                if op.get("result") is not None
                else None,
            },
        )

    def operations(
        self, *, device_id: str | None = None, since: float = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM operations WHERE created_at >= ?"
        args: list[Any] = [since]
        if device_id is not None:
            query += " AND device_id = ?"
            args.append(device_id)
        rows = self._db.execute(
            query + " ORDER BY created_at DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [_with_json(dict(row), "result") for row in rows]

    def unfinished_operations(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM operations WHERE finished_at IS NULL"
        ).fetchall()
        return [_with_json(dict(row), "result") for row in rows]

    # The voice agent's conversation, kept as it happens (for reports)

    def add_conversation_items(self, items: list[dict[str, Any]]) -> int:
        for item in items:
            self._db.execute(
                "INSERT INTO conversation_items (room, kind, role, text, data, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item["room"],
                    item["kind"],
                    item.get("role"),
                    item.get("text"),
                    json.dumps(item["data"], ensure_ascii=False)[:8000]
                    if item.get("data") is not None
                    else None,
                    item["at"],
                ),
            )
        return len(items)

    def conversation_items(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM conversation_items WHERE at >= ? AND at <= ? ORDER BY id",
            (since, until),
        ).fetchall()
        return [_with_json(dict(row), "data") for row in rows]

    def sessions_between(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM sessions WHERE created_at <= ? AND"
            " (ended_at IS NULL OR ended_at >= ?) ORDER BY created_at",
            (until, since),
        ).fetchall()
        return [dict(row) for row in rows]

    def table_since(
        self, table: str, column: str, since: float
    ) -> list[dict[str, Any]]:
        if (table, column) not in {
            ("desktop_actions", "created_at"),
            ("commands", "created_at"),
            ("events", "created_at"),
        }:
            raise ValueError("table not allowed")
        rows = self._db.execute(
            f"SELECT * FROM {table} WHERE {column} >= ? ORDER BY {column} LIMIT 2000",
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]

    # Reports about JARVIS itself (JARVIS-Notas/Relatorios)

    def reserve_report(
        self, report_id: str, name: str, device_id: str | None, note: str, now: float
    ) -> bool:
        """Claim a timestamp name; False when another report already has it."""
        try:
            self._db.execute(
                "INSERT INTO diagnostic_reports (id, name, device_id, state,"
                " created_at, note) VALUES (?, ?, ?, 'collecting', ?, ?)",
                (report_id, name, device_id, now, note[:2000]),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def update_report(self, report_id: str, **fields: Any) -> None:
        allowed = {"state", "published", "finished_at", "summary", "error"}
        if not set(fields) <= allowed:
            raise ValueError(f"unexpected report fields: {set(fields) - allowed}")
        if "summary" in fields and fields["summary"] is not None:
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._db.execute(
            f"UPDATE diagnostic_reports SET {assignments} WHERE id = ?",
            (*fields.values(), report_id),
        )

    def report(self, report_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM diagnostic_reports WHERE id = ?", (report_id,)
        ).fetchone()
        return _with_json(dict(row), "summary") if row else None

    def reports(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM diagnostic_reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_with_json(dict(row), "summary") for row in rows]

    def unpublished_reports(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM diagnostic_reports WHERE published = 0"
            " AND state IN ('ready', 'partial')"
        ).fetchall()
        return [_with_json(dict(row), "summary") for row in rows]

    def add_server_event(self, event: dict[str, Any], now: float) -> dict[str, Any]:
        """An event raised by the server itself (reports, operations, projects)."""
        row, _ = self.add_event("server", event, now)
        return row

    def prune(self, now: float, keep_seconds: float = 30 * 86400) -> None:
        cutoff = now - keep_seconds
        self._db.execute("DELETE FROM desktop_actions WHERE created_at < ?", (cutoff,))
        self._db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (cutoff,))
        self._db.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        )
        self._db.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
        self._db.execute(
            "DELETE FROM event_deliveries WHERE event_id IN"
            " (SELECT id FROM events WHERE created_at < ?)",
            (cutoff,),
        )
        self._db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        self._db.execute(
            "DELETE FROM mobile_notifications WHERE created_at < ?", (cutoff,)
        )
        self._db.execute("DELETE FROM commands WHERE created_at < ?", (now - 86400,))
        self._db.execute(
            "DELETE FROM operations WHERE finished_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        self._db.execute("DELETE FROM conversation_items WHERE at < ?", (cutoff,))
        # Diagnostic reports saved at the user's request are never pruned.


def _with_json(row: dict[str, Any], column: str) -> dict[str, Any]:
    raw = row.get(column)
    row[column] = json.loads(raw) if raw else None
    return row


def _event(row: sqlite3.Row, *, report: bool = False) -> dict[str, Any]:
    """An event for the phone; the full report only when asked for."""
    event = dict(row)
    event["data"] = json.loads(event["data"]) if event["data"] else {}
    event["cursor"] = str(event.pop("seq"))
    full = event.pop("report")
    event["has_report"] = bool(full)
    if report:
        event["report"] = full
    return event


def _device(row: sqlite3.Row) -> Device:
    return Device(
        row["id"],
        row["kind"],
        row["name"],
        row["created_at"],
        row["last_seen_at"],
        row["revoked_at"],
    )


def now() -> float:
    return time.time()
