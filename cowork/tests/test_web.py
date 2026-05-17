"""Tests for the cowork web UI (stdlib HTTP server)."""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from cowork.web import CoworkApp, _make_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str) -> tuple[int, bytes, dict]:
        r = urllib.request.urlopen(self.base + path)
        return r.status, r.read(), dict(r.headers)

    def post(self, path: str, data: dict) -> tuple[int, bytes, dict]:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req)
        return r.status, r.read(), dict(r.headers)

    def get_json(self, path: str) -> dict:
        _, body, _ = self.get(path)
        return json.loads(body)

    def post_json(self, path: str, data: dict) -> dict:
        _, body, _ = self.post(path, data)
        return json.loads(body)


@pytest.fixture(scope="module")
def client():
    port = _free_port()
    app = CoworkApp()
    server = _make_server(app, "127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield _Client(f"http://127.0.0.1:{port}")
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def seeded_client():
    """Client backed by an app pre-populated with a few memory entries."""
    port = _free_port()
    app = CoworkApp()
    app.mem.add("New product launch scheduled for Q3.", item_id="t1")
    app.mem.add("Quarterly sales were up 12% YoY.", item_id="t2")
    server = _make_server(app, "127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield _Client(f"http://127.0.0.1:{port}")
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

def test_get_root_returns_html(client):
    status, body, headers = client.get("/")
    assert status == 200
    assert b"cowork" in body
    assert b"<!doctype html>" in body.lower()
    assert "text/html" in headers.get("Content-Type", "")


def test_get_index_html_alias(client):
    status, body, _ = client.get("/index.html")
    assert status == 200
    assert b"<html" in body.lower()


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

def test_status_returns_expected_keys(client):
    d = client.get_json("/api/status")
    for key in ("tenant_id", "tenant_name", "workspace_id", "session_id", "kanban", "audit_count"):
        assert key in d, f"missing key: {key}"


def test_status_kanban_structure(client):
    kanban = client.get_json("/api/status")["kanban"]
    for key in ("pending", "claimed", "done", "failed"):
        assert key in kanban


def test_status_seeded_tasks_done(client):
    d = client.get_json("/api/status")
    assert d["kanban"]["done"] >= 4  # seeded with 4 tasks at init


def test_status_audit_count_positive(client):
    assert client.get_json("/api/status")["audit_count"] >= 1


# ---------------------------------------------------------------------------
# /api/connectors
# ---------------------------------------------------------------------------

def test_connectors_returns_list(client):
    d = client.get_json("/api/connectors")
    assert "connectors" in d
    assert isinstance(d["connectors"], list)


def test_connectors_includes_expected(client):
    names = client.get_json("/api/connectors")["connectors"]
    for expected in ("ms_graph", "zoom", "wecom", "libreoffice", "docling"):
        assert expected in names, f"missing connector: {expected}"


# ---------------------------------------------------------------------------
# /api/audit
# ---------------------------------------------------------------------------

def test_audit_returns_entries(client):
    d = client.get_json("/api/audit")
    assert "entries" in d
    assert isinstance(d["entries"], list)
    assert len(d["entries"]) >= 1


def test_audit_entry_fields(client):
    entry = client.get_json("/api/audit")["entries"][0]
    for field in ("id", "at", "actor_id", "action", "target", "metadata"):
        assert field in entry


# ---------------------------------------------------------------------------
# /api/memory/search
# ---------------------------------------------------------------------------

def test_memory_search_returns_hits(seeded_client):
    d = seeded_client.post_json("/api/memory/search", {"query": "product launch", "k": 3})
    assert "hits" in d
    assert len(d["hits"]) > 0


def test_memory_search_hit_fields(seeded_client):
    hits = seeded_client.post_json("/api/memory/search", {"query": "sales"})["hits"]
    assert hits
    h = hits[0]
    assert "text" in h and "score" in h and "id" in h


def test_memory_search_empty_query(client):
    d = client.post_json("/api/memory/search", {"query": "", "k": 3})
    assert "hits" in d  # may be empty but should not error


# ---------------------------------------------------------------------------
# /api/tasks/run
# ---------------------------------------------------------------------------

def test_tasks_run_returns_done(client):
    d = client.post_json("/api/tasks/run", {"tasks": 3, "workers": 2})
    assert d["done"] == 3
    assert d["failed"] == 0


def test_tasks_run_updates_status(client):
    client.post_json("/api/tasks/run", {"tasks": 2, "workers": 1})
    status = client.get_json("/api/status")
    assert status["kanban"]["done"] >= 2


def test_tasks_run_increments_audit(client):
    before = client.get_json("/api/status")["audit_count"]
    client.post_json("/api/tasks/run", {"tasks": 1, "workers": 1})
    after = client.get_json("/api/status")["audit_count"]
    assert after > before


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------

def test_unknown_get_returns_404(client):
    try:
        client.get("/api/doesnotexist")
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_unknown_post_returns_404(client):
    try:
        client.post("/api/doesnotexist", {})
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 404
