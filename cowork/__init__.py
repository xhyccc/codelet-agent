"""Cowork: an enterprise multi-agent collaboration layer on top of codelet.

Design constraint: this package never imports from or modifies `codelet/`.
Codelet is treated as an opaque subprocess invoked via `python -m codelet`.
"""

__version__ = "0.1.0"
