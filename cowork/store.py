"""SQLite-backed multi-tenant store.

Implements row-level tenant isolation by *always* requiring `tenant_id` on
queries that touch tenant-scoped tables. The store is thread-safe via a
single connection guarded by a lock; for multi-process use, set
`check_same_thread=False` and rely on SQLite's WAL mode (enabled here).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterable, Iterator, Optional

from . import models as M


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(tenant_id, email)
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    PRIMARY KEY(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_instances (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    codelet_session_id TEXT,
    status TEXT NOT NULL,
    pid INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_at ON audit_logs(tenant_id, at);
"""


class Store:
    """Thread-safe SQLite store with tenant-scoped accessors."""

    def __init__(self, db_path: str = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.DatabaseError:
            # WAL not supported on :memory:; ignore
            pass
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- low-level ------------------------------------------------------
    @contextmanager
    def _cur(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- tenants --------------------------------------------------------
    def create_tenant(self, t: M.Tenant) -> M.Tenant:
        with self._cur() as c:
            c.execute(
                "INSERT INTO tenants(id,name,plan,created_at) VALUES(?,?,?,?)",
                (t.id, t.name, t.plan, t.created_at),
            )
        return t

    def get_tenant(self, tenant_id: str) -> Optional[M.Tenant]:
        with self._cur() as c:
            row = c.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        return M.Tenant(**dict(row)) if row else None

    # ---- users ----------------------------------------------------------
    def create_user(self, u: M.User) -> M.User:
        with self._cur() as c:
            c.execute(
                "INSERT INTO users(id,tenant_id,email,display_name,created_at) VALUES(?,?,?,?,?)",
                (u.id, u.tenant_id, u.email, u.display_name, u.created_at),
            )
        return u

    def list_users(self, tenant_id: str) -> list[M.User]:
        with self._cur() as c:
            rows = c.execute("SELECT * FROM users WHERE tenant_id=?", (tenant_id,)).fetchall()
        return [M.User(**dict(r)) for r in rows]

    # ---- workspaces -----------------------------------------------------
    def create_workspace(self, w: M.Workspace) -> M.Workspace:
        with self._cur() as c:
            c.execute(
                "INSERT INTO workspaces(id,tenant_id,name,description,created_at) VALUES(?,?,?,?,?)",
                (w.id, w.tenant_id, w.name, w.description, w.created_at),
            )
        return w

    def get_workspace(self, tenant_id: str, workspace_id: str) -> Optional[M.Workspace]:
        with self._cur() as c:
            row = c.execute(
                "SELECT * FROM workspaces WHERE tenant_id=? AND id=?",
                (tenant_id, workspace_id),
            ).fetchone()
        return M.Workspace(**dict(row)) if row else None

    def list_workspaces(self, tenant_id: str) -> list[M.Workspace]:
        with self._cur() as c:
            rows = c.execute(
                "SELECT * FROM workspaces WHERE tenant_id=? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        return [M.Workspace(**dict(r)) for r in rows]

    def add_member(self, m: M.WorkspaceMember) -> M.WorkspaceMember:
        with self._cur() as c:
            c.execute(
                "INSERT OR REPLACE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",
                (m.workspace_id, m.user_id, m.role),
            )
        return m

    def get_member_role(self, workspace_id: str, user_id: str) -> Optional[str]:
        with self._cur() as c:
            row = c.execute(
                "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (workspace_id, user_id),
            ).fetchone()
        return row["role"] if row else None

    # ---- sessions / agents ---------------------------------------------
    def create_session(self, s: M.Session) -> M.Session:
        with self._cur() as c:
            c.execute(
                "INSERT INTO sessions(id,tenant_id,workspace_id,title,status,created_at) VALUES(?,?,?,?,?,?)",
                (s.id, s.tenant_id, s.workspace_id, s.title, s.status, s.created_at),
            )
        return s

    def get_session(self, tenant_id: str, session_id: str) -> Optional[M.Session]:
        with self._cur() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE tenant_id=? AND id=?",
                (tenant_id, session_id),
            ).fetchone()
        return M.Session(**dict(row)) if row else None

    def update_session_status(self, tenant_id: str, session_id: str, status: str) -> None:
        with self._cur() as c:
            c.execute(
                "UPDATE sessions SET status=? WHERE tenant_id=? AND id=?",
                (status, tenant_id, session_id),
            )

    def create_agent(self, a: M.AgentInstance) -> M.AgentInstance:
        with self._cur() as c:
            c.execute(
                "INSERT INTO agent_instances(id,session_id,role,codelet_session_id,status,pid,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (a.id, a.session_id, a.role, a.codelet_session_id, a.status, a.pid, a.created_at),
            )
        return a

    def update_agent(
        self,
        agent_id: str,
        *,
        codelet_session_id: Optional[str] = None,
        status: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> None:
        fields = []
        vals: list = []
        if codelet_session_id is not None:
            fields.append("codelet_session_id=?")
            vals.append(codelet_session_id)
        if status is not None:
            fields.append("status=?")
            vals.append(status)
        if pid is not None:
            fields.append("pid=?")
            vals.append(pid)
        if not fields:
            return
        vals.append(agent_id)
        with self._cur() as c:
            c.execute(f"UPDATE agent_instances SET {', '.join(fields)} WHERE id=?", vals)

    def list_agents(self, session_id: str) -> list[M.AgentInstance]:
        with self._cur() as c:
            rows = c.execute(
                "SELECT * FROM agent_instances WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return [M.AgentInstance(**dict(r)) for r in rows]

    # ---- artifacts ------------------------------------------------------
    def create_artifact(self, a: M.Artifact) -> M.Artifact:
        with self._cur() as c:
            c.execute(
                "INSERT INTO artifacts(id,session_id,kind,path,title,created_at) VALUES(?,?,?,?,?,?)",
                (a.id, a.session_id, a.kind, a.path, a.title, a.created_at),
            )
        return a

    def list_artifacts(self, session_id: str) -> list[M.Artifact]:
        with self._cur() as c:
            rows = c.execute(
                "SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [M.Artifact(**dict(r)) for r in rows]

    # ---- audit ----------------------------------------------------------
    def append_audit(self, log: M.AuditLog) -> M.AuditLog:
        with self._cur() as c:
            c.execute(
                "INSERT INTO audit_logs(id,tenant_id,actor_id,action,target,metadata,at) "
                "VALUES(?,?,?,?,?,?,?)",
                (log.id, log.tenant_id, log.actor_id, log.action, log.target, log.metadata, log.at),
            )
        return log

    def list_audit(self, tenant_id: str, limit: int = 100) -> list[M.AuditLog]:
        with self._cur() as c:
            rows = c.execute(
                "SELECT * FROM audit_logs WHERE tenant_id=? ORDER BY at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [M.AuditLog(**dict(r)) for r in rows]
