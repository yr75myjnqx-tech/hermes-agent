"""
VIE Query plugin (Quark)

Registers a ``vie_query`` toolset so that the Quark profile can READ the VIE
(Value Investor's Edge) Google Sheets itself, without needing terminal/file/
code_execution access.

Why this exists: Hermes deliberately denies Quark the terminal, file, and
code_execution toolsets (scoped sandbox). The VIE shipping/offshore analytics
live in Google Sheets shared with the niccarter59@gmail.com account, but the
``vie-sheets`` skill is script-based (vie_lookup.py / sheets_read.py), so Quark
can load the skill but never run it. This plugin restores that capability as a
*scoped, read-only* tool that reads the VIE sheets via the Google Sheets API
against the shared google_token. It grants NO shell, NO file access, NO writes
to the sheets — only read-only recall of VIE coverage/valuation data.

Safety: the tool only issues read-only Google Sheets values().get() calls
against the fixed VIE spreadsheet IDs. Nothing else.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    """Requires googleapiclient + a valid google_token.json."""
    try:
        import googleapiclient  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
    except Exception:
        return False
    import os
    return os.path.exists(os.path.expanduser("~/.hermes/google_token.json"))


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return check_requirements()


def interactive_setup() -> None:
    print("VIE Query (Quark): a scoped read-only Google Sheets tool for the quark profile.")
    print("Requires ~/.hermes/google_token.json (Google OAuth for the niccarter59@gmail.com account).")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("vie_query plugin register failed: %s", e)
