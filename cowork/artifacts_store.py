"""F12 – Versioned artifact persistence (SQLite-backed).

Stores successive versions of Artifact bodies so the UI can provide a
non-destructive version-history slider (Phase 3 of the enterprise plan).
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


@dataclass
class ArtifactVersion:
    artifact_id: str
    version: int          # 0 → auto-increment on save
    body: str
    attrs: dict           # kind, title, language, …
    created_at: float = field(default_factory=_now)
    id: str = field(default_factory=lambda: _new_id("av"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "body": self.body,
            "attrs": self.attrs,
            "created_at": self.created_at,
        }


class ArtifactVersionStore:
    """SQLite-backed store for versioned artifact bodies."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS artifact_versions (
        id          TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        version     INTEGER NOT NULL,
        body        TEXT NOT NULL,
        attrs       TEXT NOT NULL DEFAULT '{}',
        created_at  REAL NOT NULL,
        UNIQUE(artifact_id, version)
    );
    CREATE INDEX IF NOT EXISTS idx_av_artifact
        ON artifact_versions(artifact_id, version);
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(self._DDL)
        self._con.commit()

    def save(self, av: ArtifactVersion) -> ArtifactVersion:
        """Persist a version; auto-increments version if av.version == 0."""
        with self._lock:
            if av.version == 0:
                cur = self._con.execute(
                    "SELECT MAX(version) FROM artifact_versions WHERE artifact_id=?",
                    (av.artifact_id,),
                )
                row = cur.fetchone()
                av.version = (row[0] or 0) + 1
            self._con.execute(
                "INSERT INTO artifact_versions(id,artifact_id,version,body,attrs,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (av.id, av.artifact_id, av.version,
                 av.body, json.dumps(av.attrs), av.created_at),
            )
            self._con.commit()
        return av

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,artifact_id,version,body,attrs,created_at"
                " FROM artifact_versions WHERE artifact_id=? ORDER BY version",
                (artifact_id,),
            )
            return [
                ArtifactVersion(
                    id=r[0], artifact_id=r[1], version=r[2],
                    body=r[3], attrs=json.loads(r[4]), created_at=r[5],
                )
                for r in cur.fetchall()
            ]

    def get_version(self, artifact_id: str, version: int) -> Optional[ArtifactVersion]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,artifact_id,version,body,attrs,created_at"
                " FROM artifact_versions WHERE artifact_id=? AND version=?",
                (artifact_id, version),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ArtifactVersion(
                id=row[0], artifact_id=row[1], version=row[2],
                body=row[3], attrs=json.loads(row[4]), created_at=row[5],
            )

    def get_latest(self, artifact_id: str) -> Optional[ArtifactVersion]:
        with self._lock:
            cur = self._con.execute(
                "SELECT id,artifact_id,version,body,attrs,created_at"
                " FROM artifact_versions WHERE artifact_id=?"
                " ORDER BY version DESC LIMIT 1",
                (artifact_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ArtifactVersion(
                id=row[0], artifact_id=row[1], version=row[2],
                body=row[3], attrs=json.loads(row[4]), created_at=row[5],
            )

    def list_artifacts(self) -> list[dict]:
        """Return a summary row per artifact_id (latest version only)."""
        with self._lock:
            cur = self._con.execute(
                "SELECT artifact_id, MAX(version), MAX(created_at)"
                " FROM artifact_versions GROUP BY artifact_id"
                " ORDER BY MAX(created_at) DESC"
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            av = self.get_latest(r[0])
            if av:
                result.append({
                    "artifact_id": av.artifact_id,
                    "version": av.version,
                    "kind": av.attrs.get("kind", "unknown"),
                    "title": av.attrs.get("title", av.artifact_id),
                    "created_at": av.created_at,
                })
        return result

    def version_count(self, artifact_id: str) -> int:
        with self._lock:
            cur = self._con.execute(
                "SELECT COUNT(*) FROM artifact_versions WHERE artifact_id=?",
                (artifact_id,),
            )
            return cur.fetchone()[0]
