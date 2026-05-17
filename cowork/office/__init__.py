"""Office tool connectors (MCP-style).

Stdlib-only adapters with mockable network seams. Each connector exposes
``name``, ``description``, ``schema`` (dict of param -> type), and an
``invoke(args: dict) -> dict`` method. They are designed to be wrapped as
codelet tools or surfaced through MCP/A2A from the parent package.

Concrete connectors:
* :class:`MicrosoftGraphConnector` — semantic-route stub mapping common
  intents to Graph endpoints (no network).
* :class:`ZoomConnector` — Server-to-Server OAuth token cache with
  auto-refresh; ``_fetch_token`` is the seam tests patch.
* :class:`WeComConnector` — access_token cache + ``send_message`` stub +
  XML callback signature verifier.
* :class:`LibreOfficeConnector` — subprocess wrapper for
  ``soffice --headless --convert-to``; reports ``available=False`` when the
  binary is missing.
* :class:`DoclingConnector` — markdown-extraction stub backed by a swappable
  ``_convert`` callable so tests can inject fixtures.
"""
from __future__ import annotations

from .registry import ConnectorRegistry  # noqa: F401
from .microsoft_graph import MicrosoftGraphConnector  # noqa: F401
from .zoom import ZoomConnector  # noqa: F401
from .wecom import WeComConnector  # noqa: F401
from .libreoffice import LibreOfficeConnector  # noqa: F401
from .docling import DoclingConnector  # noqa: F401
