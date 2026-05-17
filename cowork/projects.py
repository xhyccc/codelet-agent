"""F14 – Collaborative projects: persistent project hubs, member roles,
privacy controls, and read-only chat snapshots (Phase 5).
"""
from __future__ import annotations

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


VISIBILITY_PRIVATE = "private"
VISIBILITY_INVITED = "invited"
VISIBILITY_ORG = "org"

ROLE_VIEWER = "viewer"
ROLE_EDITOR = "editor"


@dataclass
class Project:
    tenant_id: str
    name: str
    visibility: str = VISIBILITY_PRIVATE
    instructions: str = ""
    context_files: list[str] = field(default_factory=list)
    owner_id: str = ""
    id: str = field(default_factory=lambda: _new_id("proj"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "visibility": self.visibility,
            "instructions": self.instructions,
            "context_files": self.context_files,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
        }


@dataclass
class ProjectMember:
    project_id: str
    user_id: str
    role: str = ROLE_VIEWER
    id: str = field(default_factory=lambda: _new_id("pm"))
    added_at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "role": self.role,
            "added_at": self.added_at,
        }


@dataclass
class ChatSnapshot:
    """Read-only snapshot of a chat thread up to a point in time.

    Any messages sent *after* ``created_at`` remain private; the snapshot
    body is frozen at creation and never updated (revoke to remove access).
    """
    project_id: str
    title: str
    content: str           # JSON-serialised message list up to snapshot
    artifact_ids: list[str] = field(default_factory=list)
    creator_id: str = ""
    revoked: bool = False
    id: str = field(default_factory=lambda: _new_id("snap"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "creator_id": self.creator_id,
            "artifact_count": len(self.artifact_ids),
            "revoked": self.revoked,
            "created_at": self.created_at,
        }


class ProjectStore:
    """SQLite-backed store for projects, members, and chat snapshots."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS projects (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        name          TEXT NOT NULL,
        visibility    TEXT NOT NULL DEFAULT 'private',
        instructions  TEXT NOT NULL DEFAULT '',
        context_files TEXT NOT NULL DEFAULT '[]',
        owner_id      TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_members (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        role        TEXT NOT NULL DEFAULT 'viewer',
        added_at    REAL NOT NULL,
        UNIQUE(project_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS chat_snapshots (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL,
        title        TEXT NOT NULL,
        content      TEXT NOT NULL,
        artifact_ids TEXT NOT NULL DEFAULT '[]',
        creator_id   TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        revoked      INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(self._DDL)
        self._con.commit()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, p: Project) -> Project:
        with self._lock:
            self._con.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?)",
                (p.id, p.tenant_id, p.name, p.visibility,
                 p.instructions, json.dumps(p.context_files),
                 p.owner_id, p.created_at),
            )
            self._con.commit()
        return p

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,tenant_id,name,visibility,instructions,"
                "context_files,owner_id,created_at"
                " FROM projects WHERE id=?", (project_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Project(
            id=row[0], tenant_id=row[1], name=row[2], visibility=row[3],
            instructions=row[4], context_files=json.loads(row[5]),
            owner_id=row[6], created_at=row[7],
        )

    def list_projects(self, tenant_id: str) -> list[Project]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,tenant_id,name,visibility,instructions,"
                "context_files,owner_id,created_at"
                " FROM projects WHERE tenant_id=? ORDER BY created_at DESC",
                (tenant_id,),
            )
            rows = cur.fetchall()
        return [
            Project(
                id=r[0], tenant_id=r[1], name=r[2], visibility=r[3],
                instructions=r[4], context_files=json.loads(r[5]),
                owner_id=r[6], created_at=r[7],
            )
            for r in rows
        ]

    def update_project(
        self, project_id: str,
        visibility: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> bool:
        with self._lock:
            if visibility is not None:
                self._con.execute(
                    "UPDATE projects SET visibility=? WHERE id=?",
                    (visibility, project_id),
                )
            if instructions is not None:
                self._con.execute(
                    "UPDATE projects SET instructions=? WHERE id=?",
                    (instructions, project_id),
                )
            self._con.commit()
            return self._con.execute(
                "SELECT changes()"
            ).fetchone()[0] > 0

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    def add_member(self, member: ProjectMember) -> ProjectMember:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO project_members VALUES(?,?,?,?,?)",
                (member.id, member.project_id, member.user_id,
                 member.role, member.added_at),
            )
            self._con.commit()
        return member

    def get_member_role(self, project_id: str, user_id: str) -> Optional[str]:
        with self._lock:
            cur = self._con.execute(
                "SELECT role FROM project_members"
                " WHERE project_id=? AND user_id=?",
                (project_id, user_id),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def list_members(self, project_id: str) -> list[ProjectMember]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,project_id,user_id,role,added_at"
                " FROM project_members WHERE project_id=?",
                (project_id,),
            )
            rows = cur.fetchall()
        return [
            ProjectMember(id=r[0], project_id=r[1], user_id=r[2],
                          role=r[3], added_at=r[4])
            for r in rows
        ]

    def can_edit(self, project_id: str, user_id: str) -> bool:
        p = self.get_project(project_id)
        if p and p.owner_id == user_id:
            return True
        role = self.get_member_role(project_id, user_id)
        return role == ROLE_EDITOR

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def create_snapshot(self, snap: ChatSnapshot) -> ChatSnapshot:
        with self._lock:
            self._con.execute(
                "INSERT INTO chat_snapshots VALUES(?,?,?,?,?,?,?,?)",
                (snap.id, snap.project_id, snap.title, snap.content,
                 json.dumps(snap.artifact_ids), snap.creator_id,
                 snap.created_at, int(snap.revoked)),
            )
            self._con.commit()
        return snap

    def get_snapshot(self, snap_id: str) -> Optional[ChatSnapshot]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,project_id,title,content,artifact_ids,"
                "creator_id,created_at,revoked"
                " FROM chat_snapshots WHERE id=?", (snap_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ChatSnapshot(
            id=row[0], project_id=row[1], title=row[2], content=row[3],
            artifact_ids=json.loads(row[4]), creator_id=row[5],
            created_at=row[6], revoked=bool(row[7]),
        )

    def revoke_snapshot(self, snap_id: str) -> bool:
        with self._lock:
            self._con.execute(
                "UPDATE chat_snapshots SET revoked=1 WHERE id=?", (snap_id,)
            )
            self._con.commit()
        return True

    def list_snapshots(self, project_id: str) -> list[ChatSnapshot]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,project_id,title,content,artifact_ids,"
                "creator_id,created_at,revoked"
                " FROM chat_snapshots WHERE project_id=?"
                " ORDER BY created_at DESC",
                (project_id,),
            )
            rows = cur.fetchall()
        return [
            ChatSnapshot(
                id=r[0], project_id=r[1], title=r[2], content=r[3],
                artifact_ids=json.loads(r[4]), creator_id=r[5],
                created_at=r[6], revoked=bool(r[7]),
            )
            for r in rows
        ]
