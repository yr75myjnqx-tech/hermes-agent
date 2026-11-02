"""
GBrain Query plugin (HerbAI)

Registers a single ``gbrain_query`` tool in the ``gbrain_query`` toolset so
that the HerbAI profile can READ the Harvest-Aegis GBrain knowledge base
itself, without needing terminal/file/code_execution access.

Why this exists: Hermes deliberately denies Herb the terminal, file, and
code_execution toolsets (scoped sandbox). The GBrain knowledge base is served
by a read-only JSON API on localhost:5052, but the ``gbrain-query`` skill is
curl-based, so Herb can load it but never run it. This plugin restores that
capability as a *scoped, read-only* tool that GETs the GBrain /v1 API against
fixed localhost. It grants NO shell, NO file access, NO writes to GBrain —
only semantic recall of previously-ingested research.

Safety: the tool only issues GET requests to http://127.0.0.1:5052/v1/* and
http://127.0.0.1:5052/openapi.json. Nothing else.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    """No external deps — stdlib urllib only."""
    return True


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return True


def interactive_setup() -> None:
    print("GBrain Query (HerbAI): a scoped read-only knowledge-base tool for the herbai profile.")
    print("Nothing to configure interactively — endpoint is fixed to localhost:5052.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("gbrain_query plugin register failed: %s", e)
