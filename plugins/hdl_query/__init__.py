"""
HDL Query plugin — scoped READ-ONLY access to the Hermes Document Library.

hdl_search(q)      — keyword search over the HDL catalogue (title/path/project/agent/doc_type)
hdl_read(path)     — read one document's text (path-guarded inside the library root)
hdl_list(zone)     — list catalogue entries under a zone/folder

Why this exists: the EdBrain profile is deliberately sandboxed (no terminal,
no file toolset). Nic granted it read access to the HDL (the agents' shared
document library on MiniPro). This plugin exposes exactly that — read-only,
path-guarded, fail-closed — and nothing else.

Safety:
- Reads ONLY under ~/harvest7/docs-library/ (resolved; traversal refused).
- Text files only; 400 KB read cap with head+tail window for big docs.
- No writes, no deletes, no shell, no network.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_HDL_ROOT = Path.home() / "harvest7" / "docs-library"
_CATALOGUE = _HDL_ROOT / "_index" / "catalogue.jsonl"
_MAX_READ = 400_000
_SKIP_EVENTS = {"duplicate_retired", "retired", "retire"}

__all__ = ["register"]


def check_requirements() -> bool:
    return _CATALOGUE.exists()


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return True


def interactive_setup() -> None:
    print("HDL Query: read-only access to the Hermes Document Library catalogue + documents.")
    print("Nothing to configure — the root is fixed and fail-closed.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("hdl_query plugin register failed: %s", e)
