"""Tests for office connectors."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cowork.office import (
    ConnectorRegistry,
    DoclingConnector,
    LibreOfficeConnector,
    MicrosoftGraphConnector,
    WeComConnector,
    ZoomConnector,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_register_and_list():
    reg = ConnectorRegistry()
    reg.register(MicrosoftGraphConnector())
    reg.register(ZoomConnector())
    assert sorted(reg.names()) == ["ms_graph", "zoom"]
    tools = reg.as_tool_list()
    assert len(tools) == 2
    assert all("name" in t and "parameters" in t for t in tools)


def test_registry_invoke_dispatches():
    reg = ConnectorRegistry()
    reg.register(MicrosoftGraphConnector())
    res = reg.invoke("ms_graph", {"query": "show my unread emails"})
    assert res["ok"] is True
    assert res["route"]["endpoint"] == "/me/messages"


def test_registry_rejects_duplicate():
    reg = ConnectorRegistry()
    reg.register(MicrosoftGraphConnector())
    with pytest.raises(ValueError):
        reg.register(MicrosoftGraphConnector())


# ---------------------------------------------------------------------------
# Microsoft Graph
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q,endpoint", [
    ("show my unread emails", "/me/messages"),
    ("what's on my calendar today", "/me/events"),
    ("list my onedrive files", "/me/drive/root/children"),
    ("my teams", "/me/joinedTeams"),
    ("find people named Alex", "/me/contacts"),
    ("something else entirely", "/me"),
])
def test_graph_routes(q, endpoint):
    c = MicrosoftGraphConnector()
    assert c.route(q)["endpoint"] == endpoint


def test_graph_missing_query():
    assert MicrosoftGraphConnector().invoke({})["ok"] is False


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------

def test_zoom_caches_token():
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return (f"t{calls['n']}", 3600.0)

    z = ZoomConnector(_fetch_token=fake_fetch)
    a = z.get_token()
    b = z.get_token()
    assert a == b == "t1"
    assert calls["n"] == 1


def test_zoom_refreshes_when_expiring():
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return (f"t{calls['n']}", 200.0)

    z = ZoomConnector(_fetch_token=fake_fetch)
    z.get_token(now=0)
    z.get_token(now=0)       # cached
    z.get_token(now=300)     # expired -> refresh
    assert calls["n"] == 2


def test_zoom_create_meeting():
    z = ZoomConnector()
    res = z.invoke({"action": "create_meeting", "topic": "Sync", "start_time": "2025-01-01T10:00:00Z"})
    assert res["ok"] is True
    assert res["meeting"]["topic"] == "Sync"


def test_zoom_create_meeting_missing_field():
    z = ZoomConnector()
    res = z.invoke({"action": "create_meeting", "topic": "x"})
    assert res["ok"] is False and "missing" in res["error"]


# ---------------------------------------------------------------------------
# WeCom
# ---------------------------------------------------------------------------

def test_wecom_signature_roundtrip():
    w = WeComConnector(token="demo-token")
    sig = w.make_signature(timestamp="100", nonce="abc", echostr="hello")
    assert w.verify_signature(signature=sig, timestamp="100", nonce="abc", echostr="hello")
    # tampered
    assert not w.verify_signature(signature=sig, timestamp="999", nonce="abc", echostr="hello")


def test_wecom_send_message_records():
    w = WeComConnector()
    res = w.invoke({"action": "send_message", "to_user": "u1", "content": "hi"})
    assert res["ok"]
    assert len(w._sent) == 1
    assert w._sent[0]["to_user"] == "u1"


def test_wecom_unknown_action():
    assert WeComConnector().invoke({"action": "explode"})["ok"] is False


# ---------------------------------------------------------------------------
# LibreOffice
# ---------------------------------------------------------------------------

def test_libreoffice_reports_unavailable_when_binary_missing():
    c = LibreOfficeConnector(binary="definitely-not-a-real-binary-xyz")
    assert c.available is False
    res = c.invoke({"input_path": "/tmp/x.docx", "output_format": "pdf"})
    assert res["ok"] is False
    assert "not found" in res["error"]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_libreoffice_real_conversion(tmp_path: Path):
    # Smoke test only when soffice is present.
    src = tmp_path / "hello.txt"
    src.write_text("Hello, world.")
    c = LibreOfficeConnector()
    res = c.invoke({"input_path": str(src), "output_format": "pdf", "output_dir": str(tmp_path)})
    # Either succeeds or fails gracefully; we just want a structured dict.
    assert isinstance(res, dict) and "ok" in res


# ---------------------------------------------------------------------------
# Docling
# ---------------------------------------------------------------------------

def test_docling_default_reads_text_file(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("# hello")
    res = DoclingConnector().invoke({"path": str(p)})
    assert res["ok"] is True
    assert res["markdown"] == "# hello"


def test_docling_missing_path():
    assert DoclingConnector().invoke({})["ok"] is False


def test_docling_custom_convert():
    def fake(path):
        return f"# Fake({path.name})"

    c = DoclingConnector(_convert=fake)
    res = c.extract("/x/y/z.pdf")
    assert res["ok"] and res["markdown"] == "# Fake(z.pdf)"
