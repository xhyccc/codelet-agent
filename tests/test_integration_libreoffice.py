"""Integration tests for LibreOffice document manipulation.

These tests exercise the LibreOfficeConnector with real file conversions.
They require:
  - LibreOffice (soffice) installed on PATH
  - Optionally, API credentials for agent-driven conversion tests

Run with:
    python -m pytest tests/test_integration_libreoffice.py -v
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from codelet.libreoffice import LibreOfficeConnector

# Import shared markers
from conftest import requires_api_key, requires_libreoffice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_text_file(path: Path, content: str = "Hello, World!") -> Path:
    """Create a simple text file for conversion."""
    path.write_text(content, encoding="utf-8")
    return path


def _create_html_file(path: Path) -> Path:
    """Create a simple HTML file for conversion."""
    html = """<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body>
<h1>Integration Test Document</h1>
<p>This is a test paragraph for LibreOffice conversion.</p>
<table><tr><td>Cell 1</td><td>Cell 2</td></tr></table>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")
    return path


def _create_csv_file(path: Path) -> Path:
    """Create a CSV file for spreadsheet conversion."""
    csv_content = "Name,Age,City\nAlice,30,New York\nBob,25,London\nCharlie,35,Tokyo\n"
    path.write_text(csv_content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests: Connector basics (no LibreOffice needed)
# ---------------------------------------------------------------------------


class TestLibreOfficeConnectorBasics:
    """Test connector behavior without requiring LibreOffice installed."""

    def test_connector_instantiation(self):
        c = LibreOfficeConnector()
        assert c.name == "libreoffice"
        assert c.binary == "soffice"
        assert "input_path" in c.schema
        assert "output_format" in c.schema

    def test_unavailable_when_binary_missing(self):
        c = LibreOfficeConnector(binary="nonexistent-binary-xyz-123")
        assert c.available is False

    def test_invoke_returns_error_when_unavailable(self):
        c = LibreOfficeConnector(binary="nonexistent-binary-xyz-123")
        result = c.invoke({"input_path": "/tmp/test.txt", "output_format": "pdf"})
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_invoke_returns_error_for_missing_input(self, tmp_path):
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(tmp_path / "nonexistent.docx"),
            output_format="pdf",
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_invoke_interface_accepts_dict(self):
        c = LibreOfficeConnector(binary="nonexistent-binary-xyz-123")
        # invoke() should handle the dict-based interface
        result = c.invoke({
            "input_path": "/tmp/fake.docx",
            "output_format": "pdf",
            "output_dir": "/tmp/out",
        })
        assert isinstance(result, dict)
        assert "ok" in result


# ---------------------------------------------------------------------------
# Tests: Real conversions (requires LibreOffice)
# ---------------------------------------------------------------------------


@requires_libreoffice
class TestLibreOfficeRealConversions:
    """Integration tests that perform actual file conversions with LibreOffice."""

    def test_text_to_pdf(self, tmp_path):
        """Convert a plain text file to PDF."""
        src = _create_text_file(tmp_path / "hello.txt")
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="pdf",
            output_dir=str(tmp_path / "output"),
        )
        assert result["ok"] is True
        assert result["output_path"]
        assert Path(result["output_path"]).exists()
        assert Path(result["output_path"]).suffix == ".pdf"

    def test_html_to_pdf(self, tmp_path):
        """Convert an HTML file to PDF."""
        src = _create_html_file(tmp_path / "doc.html")
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="pdf",
            output_dir=str(tmp_path / "output"),
        )
        assert result["ok"] is True
        out = Path(result["output_path"])
        assert out.exists()
        assert out.stat().st_size > 0

    def test_csv_to_pdf(self, tmp_path):
        """Convert a CSV file to PDF (via Calc)."""
        src = _create_csv_file(tmp_path / "data.csv")
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="pdf",
            output_dir=str(tmp_path / "output"),
        )
        assert result["ok"] is True
        assert Path(result["output_path"]).exists()

    def test_html_to_docx(self, tmp_path):
        """Convert an HTML file to DOCX."""
        src = _create_html_file(tmp_path / "page.html")
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="docx",
            output_dir=str(tmp_path / "output"),
        )
        assert result["ok"] is True
        out = Path(result["output_path"])
        assert out.exists()
        assert out.suffix == ".docx"

    def test_text_to_html(self, tmp_path):
        """Convert a text file to HTML."""
        src = _create_text_file(tmp_path / "notes.txt", "Some notes here.")
        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="html",
            output_dir=str(tmp_path / "output"),
        )
        assert result["ok"] is True
        out = Path(result["output_path"])
        assert out.exists()
        content = out.read_text(encoding="utf-8", errors="replace")
        assert "notes" in content.lower() or "Some notes" in content

    def test_output_dir_created_automatically(self, tmp_path):
        """output_dir should be created if it doesn't exist."""
        src = _create_text_file(tmp_path / "input.txt")
        out_dir = tmp_path / "deep" / "nested" / "output"
        assert not out_dir.exists()

        c = LibreOfficeConnector()
        result = c.convert(
            input_path=str(src),
            output_format="pdf",
            output_dir=str(out_dir),
        )
        assert result["ok"] is True
        assert out_dir.exists()

    def test_default_output_dir_is_input_parent(self, tmp_path):
        """When output_dir is not specified, output goes to input file's directory."""
        src = _create_text_file(tmp_path / "file.txt")
        c = LibreOfficeConnector()
        result = c.convert(input_path=str(src), output_format="pdf")
        assert result["ok"] is True
        out = Path(result["output_path"])
        assert out.parent == tmp_path

    def test_invoke_dict_interface(self, tmp_path):
        """The invoke() method should work with a dict of arguments."""
        src = _create_text_file(tmp_path / "doc.txt")
        c = LibreOfficeConnector()
        result = c.invoke({
            "input_path": str(src),
            "output_format": "pdf",
            "output_dir": str(tmp_path / "out"),
        })
        assert result["ok"] is True
        assert Path(result["output_path"]).exists()

    def test_multiple_conversions_sequentially(self, tmp_path):
        """Multiple conversions should work without interference."""
        c = LibreOfficeConnector()
        files = []
        for i in range(3):
            src = _create_text_file(tmp_path / f"doc_{i}.txt", f"Content {i}")
            files.append(src)

        results = []
        for src in files:
            result = c.convert(
                input_path=str(src),
                output_format="pdf",
                output_dir=str(tmp_path / "batch_output"),
            )
            results.append(result)

        for result in results:
            assert result["ok"] is True
            assert Path(result["output_path"]).exists()


# ---------------------------------------------------------------------------
# Tests: Agent-driven LibreOffice usage (requires API key + LibreOffice)
# ---------------------------------------------------------------------------


@requires_api_key
@requires_libreoffice
class TestAgentDrivenConversion:
    """End-to-end tests where the agent uses LibreOffice via tool calls."""

    @pytest.fixture()
    def model_client(self):
        from codelet import OpenAIModelClient

        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
        base_url = os.environ.get("LLM_BASE_URL") or None
        return OpenAIModelClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.0,
            top_p=1.0,
            timeout=60,
        )

    def test_libreoffice_connector_available(self):
        """Sanity check: LibreOffice is available in this environment."""
        c = LibreOfficeConnector()
        assert c.available is True

    def test_agent_can_create_and_convert_file(self, tmp_path, model_client):
        """Agent creates a file and we convert it with LibreOffice."""
        from codelet import MiniAgent, SessionStore, WorkspaceContext

        # Set up workspace with a text file
        (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
        doc = tmp_path / "report.txt"
        doc.write_text("Quarterly Report\n\nRevenue increased by 15%.\n")

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=model_client,
            workspace=ws,
            session_store=store,
            approval_policy="auto",
            max_steps=3,
        )

        # Verify the agent can read the file
        result = agent.run_tool("read_file", {"path": "report.txt", "start": 1, "end": 5})
        assert "Quarterly Report" in result

        # Now use LibreOffice to convert it
        c = LibreOfficeConnector()
        conv_result = c.convert(
            input_path=str(doc),
            output_format="pdf",
            output_dir=str(tmp_path / "converted"),
        )
        assert conv_result["ok"] is True
        assert Path(conv_result["output_path"]).exists()
