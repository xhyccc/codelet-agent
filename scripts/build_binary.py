#!/usr/bin/env python3
"""Build script to create a standalone codelet binary executable."""

import os
import sys
import subprocess
import shutil
from pathlib import Path

# Paths
ROOT = Path(__file__).parent.parent.resolve()
CODELET_DIR = ROOT / "codelet"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"

# Entry point script (generated)
ENTRY_SCRIPT = ROOT / "build_entrypoint.py"

# Data files to bundle
DATA_PATTERNS = [
    ("codelet/config", "codelet/config"),
    ("codelet/protocols", "codelet/protocols"),
]

# Hidden imports that PyInstaller might miss
HIDDEN_IMPORTS = [
    "codelet.cli",
    "codelet.agent",
    "codelet.tools",
    "codelet.config",
    "codelet.clients",
    "codelet.providers",
    "codelet.sandbox",
    "codelet.baseline",
    "codelet.compaction",
    "codelet.memory_files",
    "codelet.parsing",
    "codelet.prompt",
    "codelet.sessions",
    "codelet.utils",
    "codelet.workspace",
    "codelet.welcome",
    "codelet.env_config",
    "codelet.hardening",
    "codelet.libreoffice",
    "codelet.cost_tracker",
    "codelet.permissions",
    "codelet.history",
    "codelet.commands",
    "codelet.tasks",
    "codelet.memdir",
    "codelet.query",
    "codelet.file_history",
    "codelet.subagent",
    "codelet.mcp",
    "codelet.abort_controller",
    "codelet.stop_reason",
    "codelet.skills",
    "openai",
    "rich",
    "rich.console",
    "rich.panel",
    "rich.markdown",
    "rich.syntax",
    "rich.live",
    "rich.spinner",
    "rich.prompt",
    "rich.table",
    "rich.text",
    "rich.layout",
    "rich.align",
    "prompt_toolkit",
    "prompt_toolkit.shortcuts",
    "prompt_toolkit.formatted_text",
    "prompt_toolkit.styles",
    "yaml",
    "pytest",
    "playwright",
    "playwright.sync_api",
]


def generate_entrypoint():
    """Generate a single-file entrypoint for PyInstaller."""
    script = '''#!/usr/bin/env python3
"""Standalone entrypoint for codelet binary."""

import sys
import os

# Ensure bundled resources are findable
if getattr(sys, "frozen", False):
    # Running as compiled binary
    bundle_dir = sys._MEIPASS
    os.environ.setdefault("CODELET_BUNDLE_DIR", bundle_dir)

# Import and run
from codelet.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'''
    ENTRY_SCRIPT.write_text(script, encoding="utf-8")
    print(f"Generated entrypoint: {ENTRY_SCRIPT}")


def build_binary():
    """Run PyInstaller to build the binary."""
    # Clean previous builds
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)

    # Build command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "codelet",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--console",
    ]

    # Add hidden imports
    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # Add data files
    for src, dst in DATA_PATTERNS:
        src_path = ROOT / src
        if src_path.exists():
            cmd.extend(["--add-data", f"{src_path}:{dst}"])

    # Add the entrypoint
    cmd.append(str(ENTRY_SCRIPT))

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode != 0:
        print("ERROR: PyInstaller build failed")
        return False

    # Verify output
    binary = DIST_DIR / ("codelet.exe" if sys.platform == "win32" else "codelet")
    if binary.exists():
        size_mb = binary.stat().st_size / (1024 * 1024)
        print(f"\n✅ Build successful!")
        print(f"   Binary: {binary}")
        print(f"   Size: {size_mb:.1f} MB")
        print(f"\n   Run it:")
        print(f"   {binary} --help")
        return True
    else:
        print("ERROR: Binary not found after build")
        return False


def main():
    print("=" * 60)
    print("Building codelet standalone binary")
    print("=" * 60)

    generate_entrypoint()
    success = build_binary()

    # Cleanup
    if ENTRY_SCRIPT.exists():
        ENTRY_SCRIPT.unlink()

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
