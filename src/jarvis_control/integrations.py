"""Personal connection preferences and state. Credentials belong to n8n, never this API.

An account is "connected" only after a successful identity and service check made
through n8n (personal.verify); a saved credential or a healthy n8n proves nothing.
The verified identity and the checks stay private: the app sees only the status.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from typing import Any

CATALOG = {
    "google_a": ("google", "Google A", ["calendar", "drive", "gmail"]),
    "google_b": ("google", "Google B", ["calendar", "drive"]),
    "outlook": ("microsoft", "Outlook", ["mail"]),
    "notion": ("notion", "Notion", ["tasks"]),
}
STATES = {
    "setup_required",
    "authorizing",
    "connected",
    "partial",
    "expired",
    "error",
    "disconnected",
}
ALLOWED_SERVICES = {
    "google": {"calendar", "drive", "gmail"},
    "microsoft": {"mail"},
    "notion": {"tasks"},
}


class IntegrationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class Integrations:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS personal_connections (
                id TEXT PRIMARY KEY, label TEXT NOT NULL,
                enabled INTEGER NOT NULL, services TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_personal_access (
                device_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS personal_connection_state (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, checked_at REAL,
                detail TEXT, identity TEXT, services TEXT, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personal_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        # Set by serve() when the n8n gateway URL and secret are configured.
        self.configured = False
        for key, (_, label, services) in CATALOG.items():
            db.execute(
                "INSERT OR IGNORE INTO personal_connections VALUES (?, ?, 1, ?, 1, ?)",
                (key, label, json.dumps(services), time.time()),
            )

    # Runtime switch and connection state (admin commands and the gateway)

    @property
    def runtime_enabled(self) -> bool:
        row = self.db.execute(
            "SELECT value FROM personal_settings WHERE key = 'runtime'"
        ).fetchone()
        return self.configured and bool(row and row["value"] == "on")

    def set_runtime(self, enabled: bool) -> None:
        self.db.execute(
            "INSERT INTO personal_settings VALUES ('runtime', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("on" if enabled else "off",),
        )

    def state(self, account_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM personal_connection_state WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return {
                "state": "setup_required",
                "checked_at": None,
                "detail": None,
                "identity": None,
                "services": [],
            }
        return {
            "state": row["state"],
            "checked_at": row["checked_at"],
            "detail": row["detail"],
            "identity": row["identity"],
            "services": json.loads(row["services"] or "[]"),
        }

    def set_state(
        self,
        account_id: str,
        state: str,
        *,
        detail: str | None = None,
        identity: str | None = None,
        services: list[str] | None = None,
        checked: bool = False,
        keep_identity: bool = True,
    ) -> None:
        if account_id not in CATALOG or state not in STATES:
            raise ValueError(f"unknown account or state: {account_id} {state}")
        now = time.time()
        previous = self.state(account_id)
        self.db.execute(
            "INSERT INTO personal_connection_state VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
            " checked_at=excluded.checked_at, detail=excluded.detail,"
            " identity=excluded.identity, services=excluded.services,"
            " updated_at=excluded.updated_at",
            (
                account_id,
                state,
                now if checked else previous["checked_at"],
                detail,
                identity or (previous["identity"] if keep_identity else None),
                json.dumps(services if services is not None else previous["services"]),
                now,
            ),
        )

    def account(self, account_id: str) -> dict[str, Any]:
        provider = CATALOG[account_id][0]
        row = self.db.execute(
            "SELECT * FROM personal_connections WHERE id = ?", (account_id,)
        ).fetchone()
        state = self.state(account_id)
        return {
            "id": account_id,
            "provider": provider,
            "label": row["label"],
            "enabled": bool(row["enabled"]),
            "services": json.loads(row["services"]),
            # Pausing is the user's choice in the app and keeps the authorization;
            # the underlying state comes back when the account is resumed.
            "status": state["state"] if row["enabled"] else "paused",
            "checked_at": state["checked_at"],
            "verified_services": state["services"],
            "detail": state["detail"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        }

    def usable(self, service: str) -> list[dict[str, Any]]:
        """Accounts the gateway may query for a service right now."""
        if not self.runtime_enabled:
            return []
        result = []
        for key in CATALOG:
            account = self.account(key)
            state = self.state(key)
            if (
                account["status"] in {"connected", "partial"}
                and service in account["services"]
                and service in state["services"]
            ):
                result.append(account)
        return result

    def view(self) -> dict[str, Any]:
        runtime = self.runtime_enabled
        return {
            "schema_version": 1,
            "phase": "active" if runtime else "configuration",
            "runtime_enabled": runtime,
            "read_only": True,
            "accounts": [self.account(key) for key in CATALOG],
            "message": (
                "Consultas de leitura ativas para as contas conectadas."
                if runtime
                else "Preferências disponíveis. A autorização das contas e as"
                " consultas ainda estão em preparação."
            ),
        }

    def update(self, account_id: str, body: Any) -> dict[str, Any]:
        if account_id not in CATALOG:
            raise IntegrationError("not_found", "Conexão desconhecida.", 404)
        if (
            not isinstance(body, dict)
            or not body
            or set(body) - {"revision", "label", "enabled", "services"}
        ):
            raise IntegrationError(
                "invalid_fields", "Envie apenas apelido, serviços, ativação e revisão."
            )
        if type(body.get("revision")) is not int or body["revision"] < 1:
            raise IntegrationError(
                "invalid_revision", "Atualize a tela antes de salvar."
            )
        if "label" in body:
            label = body["label"]
            if (
                not isinstance(label, str)
                or not 1 <= len(label.strip()) <= 60
                or any(unicodedata.category(c).startswith("C") for c in label)
            ):
                raise IntegrationError(
                    "invalid_label",
                    "Use um apelido de 1 a 60 caracteres, sem controles.",
                )
        if "enabled" in body and type(body["enabled"]) is not bool:
            raise IntegrationError(
                "invalid_enabled", "Ativação precisa ser verdadeiro ou falso."
            )
        provider = CATALOG[account_id][0]
        if "services" in body:
            services = body["services"]
            if (
                not isinstance(services, list)
                or not services
                or len(services) > 3
                or any(not isinstance(s, str) for s in services)
                or len(set(services)) != len(services)
                or not set(services) <= ALLOWED_SERVICES[provider]
            ):
                raise IntegrationError(
                    "invalid_services", "Selecione serviços válidos para esta conta."
                )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM personal_connections WHERE id = ?", (account_id,)
            ).fetchone()
            if body["revision"] != row["revision"]:
                raise IntegrationError(
                    "revision_conflict",
                    "Esta conexão mudou em outro aparelho. Atualize e tente novamente.",
                    409,
                )
            services = body.get("services", json.loads(row["services"]))
            if "gmail" in services:
                others = self.db.execute(
                    "SELECT services FROM personal_connections WHERE id != ?",
                    (account_id,),
                )
                if any("gmail" in json.loads(other["services"]) for other in others):
                    raise IntegrationError(
                        "gmail_account_conflict",
                        "Desmarque Gmail na outra conta antes de selecionar esta.",
                        409,
                    )
            self.db.execute(
                "UPDATE personal_connections SET label=?, enabled=?, services=?, revision=revision+1, updated_at=? WHERE id=?",
                (
                    body.get("label", row["label"]).strip(),
                    int(body.get("enabled", bool(row["enabled"]))),
                    json.dumps(services),
                    time.time(),
                    account_id,
                ),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.view()
