"""LibreOffice headless conversion connector.

Wraps ``soffice --headless --convert-to`` for docx/xlsx/pptx -> pdf/html
conversions. If ``soffice`` is not on PATH, ``available`` is False and
``invoke`` returns a structured error rather than raising.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class LibreOfficeConnector:
    binary: str = "soffice"
    name: str = "libreoffice"
    description: str = "Convert Office documents using LibreOffice in headless mode."
    schema: dict[str, str] = field(default_factory=lambda: {
        "input_path": "string",
        "output_format": "string",
        "output_dir": "string",
    })

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def convert(
        self,
        *,
        input_path: str,
        output_format: str = "pdf",
        output_dir: Optional[str] = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": f"{self.binary!r} not found on PATH"}
        ipath = Path(input_path)
        if not ipath.exists():
            return {"ok": False, "error": f"input not found: {input_path}"}
        odir = Path(output_dir) if output_dir else ipath.parent
        odir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [self.binary, "--headless", "--convert-to", output_format,
                 "--outdir", str(odir), str(ipath)],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "conversion timed out"}
        out_path = odir / (ipath.stem + "." + output_format.split(":", 1)[0])
        return {
            "ok": proc.returncode == 0 and out_path.exists(),
            "output_path": str(out_path) if out_path.exists() else "",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.convert(
            input_path=args.get("input_path", ""),
            output_format=args.get("output_format", "pdf"),
            output_dir=args.get("output_dir"),
        )
