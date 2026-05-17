"""F15 – Enterprise admin: SSO config, RBAC capability matrix,
SCIM group sync, OpenTelemetry-style event recording, and compliance export
(Phase 6 of the enterprise plan).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# SSO Configuration
# ---------------------------------------------------------------------------

@dataclass
class SSOConfig:
    provider: str           # "saml" | "oidc"
    metadata_url: str = ""
    client_id: str = ""
    client_secret: str = ""   # stored hashed in a real deployment
    require_sso: bool = True
    allowed_domains: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("sso"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "metadata_url": self.metadata_url,
            "client_id": self.client_id,
            "require_sso": self.require_sso,
            "allowed_domains": self.allowed_domains,
        }


# ---------------------------------------------------------------------------
# RBAC capability matrix
# ---------------------------------------------------------------------------

CAPABILITY_CHAT = "chat"
CAPABILITY_TASKS = "tasks"
CAPABILITY_CLI = "cli"
CAPABILITY_API_KEYS = "api_keys"
CAPABILITY_WEB_SEARCH = "web_search"
CAPABILITY_COMPUTER_USE = "computer_use"
CAPABILITY_FILE_DOWNLOAD = "file_download"
CAPABILITY_SESSION_TRACING = "session_tracing"
CAPABILITY_ANALYTICS = "analytics"
CAPABILITY_BILLING = "billing"
CAPABILITY_ADMIN = "admin"

ALL_CAPABILITIES: list[str] = [
    CAPABILITY_CHAT, CAPABILITY_TASKS, CAPABILITY_CLI,
    CAPABILITY_API_KEYS, CAPABILITY_WEB_SEARCH, CAPABILITY_COMPUTER_USE,
    CAPABILITY_FILE_DOWNLOAD, CAPABILITY_SESSION_TRACING,
    CAPABILITY_ANALYTICS, CAPABILITY_BILLING, CAPABILITY_ADMIN,
]

_DEFAULT_CAPS: dict[str, set[str]] = {
    "workspace_user": {
        CAPABILITY_CHAT,
    },
    "workspace_limited_developer": {
        CAPABILITY_CHAT, CAPABILITY_TASKS, CAPABILITY_API_KEYS,
    },
    "workspace_developer": {
        CAPABILITY_CHAT, CAPABILITY_TASKS, CAPABILITY_CLI,
        CAPABILITY_API_KEYS, CAPABILITY_WEB_SEARCH, CAPABILITY_COMPUTER_USE,
        CAPABILITY_ANALYTICS,
    },
    "workspace_admin": {
        CAPABILITY_CHAT, CAPABILITY_TASKS, CAPABILITY_CLI,
        CAPABILITY_API_KEYS, CAPABILITY_WEB_SEARCH, CAPABILITY_COMPUTER_USE,
        CAPABILITY_FILE_DOWNLOAD, CAPABILITY_SESSION_TRACING,
        CAPABILITY_ANALYTICS, CAPABILITY_ADMIN,
    },
}


@dataclass
class RBACRole:
    name: str
    capabilities: set[str] = field(default_factory=set)
    is_custom: bool = False
    id: str = field(default_factory=lambda: _new_id("role"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": sorted(self.capabilities),
            "is_custom": self.is_custom,
        }


# ---------------------------------------------------------------------------
# SCIM group
# ---------------------------------------------------------------------------

@dataclass
class SCIMGroup:
    external_id: str
    name: str
    mapped_role: str
    id: str = field(default_factory=lambda: _new_id("scim"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "name": self.name,
            "mapped_role": self.mapped_role,
        }


# ---------------------------------------------------------------------------
# Telemetry event
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    session_id: str
    event_type: str          # session_start | tool_use | session_end
    tokens_used: int = 0
    duration_ms: int = 0
    tools_invoked: list[str] = field(default_factory=list)
    user_id: str = ""
    id: str = field(default_factory=lambda: _new_id("tel"))
    at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "tools_invoked": self.tools_invoked,
            "user_id": self.user_id,
            "at": self.at,
        }


# ---------------------------------------------------------------------------
# AdminStore
# ---------------------------------------------------------------------------

class AdminStore:
    """SQLite-backed admin store: SSO, RBAC roles, SCIM groups, telemetry."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS sso_configs (
        id             TEXT PRIMARY KEY,
        provider       TEXT NOT NULL,
        metadata_url   TEXT NOT NULL DEFAULT '',
        client_id      TEXT NOT NULL DEFAULT '',
        client_secret  TEXT NOT NULL DEFAULT '',
        require_sso    INTEGER NOT NULL DEFAULT 1,
        allowed_domains TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS rbac_roles (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL UNIQUE,
        capabilities TEXT NOT NULL DEFAULT '[]',
        is_custom    INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS scim_groups (
        id          TEXT PRIMARY KEY,
        external_id TEXT NOT NULL,
        name        TEXT NOT NULL,
        mapped_role TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS telemetry (
        id            TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL,
        event_type    TEXT NOT NULL,
        tokens_used   INTEGER NOT NULL DEFAULT 0,
        duration_ms   INTEGER NOT NULL DEFAULT 0,
        tools_invoked TEXT NOT NULL DEFAULT '[]',
        user_id       TEXT NOT NULL DEFAULT '',
        at            REAL NOT NULL
    );
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(self._DDL)
        self._con.commit()
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        for name, caps in _DEFAULT_CAPS.items():
            if not self._con.execute(
                "SELECT 1 FROM rbac_roles WHERE name=?", (name,)
            ).fetchone():
                r = RBACRole(name=name, capabilities=caps, is_custom=False)
                self._con.execute(
                    "INSERT INTO rbac_roles VALUES(?,?,?,?)",
                    (r.id, r.name, json.dumps(sorted(r.capabilities)), 0),
                )
        self._con.commit()

    # -- SSO --

    def save_sso(self, cfg: SSOConfig) -> SSOConfig:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO sso_configs VALUES(?,?,?,?,?,?,?)",
                (cfg.id, cfg.provider, cfg.metadata_url, cfg.client_id,
                 cfg.client_secret, int(cfg.require_sso),
                 json.dumps(cfg.allowed_domains)),
            )
            self._con.commit()
        return cfg

    def get_sso(self) -> Optional[SSOConfig]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,provider,metadata_url,client_id,client_secret,"
                "require_sso,allowed_domains FROM sso_configs LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return SSOConfig(
            id=row[0], provider=row[1], metadata_url=row[2],
            client_id=row[3], client_secret=row[4],
            require_sso=bool(row[5]), allowed_domains=json.loads(row[6]),
        )

    # -- RBAC --

    def list_roles(self) -> list[RBACRole]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,name,capabilities,is_custom FROM rbac_roles"
                " ORDER BY name"
            )
            rows = cur.fetchall()
        return [
            RBACRole(id=r[0], name=r[1],
                     capabilities=set(json.loads(r[2])), is_custom=bool(r[3]))
            for r in rows
        ]

    def upsert_role(self, role: RBACRole) -> RBACRole:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO rbac_roles VALUES(?,?,?,?)",
                (role.id, role.name,
                 json.dumps(sorted(role.capabilities)), int(role.is_custom)),
            )
            self._con.commit()
        return role

    def get_role(self, name: str) -> Optional[RBACRole]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,name,capabilities,is_custom FROM rbac_roles WHERE name=?",
                (name,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return RBACRole(id=row[0], name=row[1],
                        capabilities=set(json.loads(row[2])),
                        is_custom=bool(row[3]))

    def check_capability(self, role_name: str, capability: str) -> bool:
        role = self.get_role(role_name)
        return role is not None and capability in role.capabilities

    # -- SCIM --

    def sync_group(self, group: SCIMGroup) -> SCIMGroup:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO scim_groups VALUES(?,?,?,?)",
                (group.id, group.external_id, group.name, group.mapped_role),
            )
            self._con.commit()
        return group

    def list_groups(self) -> list[SCIMGroup]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,external_id,name,mapped_role FROM scim_groups"
            )
            rows = cur.fetchall()
        return [
            SCIMGroup(id=r[0], external_id=r[1], name=r[2], mapped_role=r[3])
            for r in rows
        ]

    # -- Telemetry --

    def record(self, event: TelemetryEvent) -> TelemetryEvent:
        with self._lock:
            self._con.execute(
                "INSERT INTO telemetry VALUES(?,?,?,?,?,?,?,?)",
                (event.id, event.session_id, event.event_type,
                 event.tokens_used, event.duration_ms,
                 json.dumps(event.tools_invoked), event.user_id, event.at),
            )
            self._con.commit()
        return event

    def aggregate_usage(self, days: int = 30) -> dict:
        cutoff = _now() - days * 86400
        with self._lock:
            row = self._con.execute(
                "SELECT SUM(tokens_used), SUM(duration_ms), COUNT(*)"
                " FROM telemetry WHERE at>=?", (cutoff,)
            ).fetchone()
        return {
            "total_tokens": row[0] or 0,
            "total_duration_ms": row[1] or 0,
            "event_count": row[2] or 0,
            "period_days": days,
        }

    def event_type_breakdown(self, days: int = 30) -> dict:
        cutoff = _now() - days * 86400
        with self._lock:
            cur = self._con.execute(
                "SELECT event_type, COUNT(*) FROM telemetry"
                " WHERE at>=? GROUP BY event_type", (cutoff,)
            )
            rows = cur.fetchall()
        return {r[0]: r[1] for r in rows}

    # -- Compliance export --

    def export_audit_csv(self, days: int = 180) -> str:
        """Return telemetry as CSV (prompt content deliberately excluded)."""
        cutoff = _now() - days * 86400
        with self._lock:
            cur = self._con.execute(
                "SELECT id,session_id,event_type,tokens_used,duration_ms,"
                "tools_invoked,user_id,at FROM telemetry"
                " WHERE at>=? ORDER BY at", (cutoff,)
            )
            rows = cur.fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "event_id", "session_id", "event_type", "tokens_used",
            "duration_ms", "tools_invoked", "user_id", "timestamp",
        ])
        for r in rows:
            writer.writerow([
                r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r[7])),
            ])
        return buf.getvalue()
