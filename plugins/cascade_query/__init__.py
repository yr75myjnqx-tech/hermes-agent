"""
Cascade Query plugin (Quark)

Registers a ``cascade_query`` toolset so the Quark profile can READ the
Cascade v2 engine output — the ranked, signed, evidence-tagged candidate
lists — without needing terminal/file/code_execution access.

Why this exists: Quark is deliberately sandboxed (no terminal/file/code).
The Cascade v2 engine (SpockAI's machinery) produces a well-scoped, signed,
evidence-tagged candidate list per thesis; the final tickers + dominance
call is Quark's judgement. This plugin restores that capability as a scoped,
read-only tool that SELECTs from the harvest7 Postgres cascade tables.

Safety: the tool only issues read-only SELECTs against the harvest7
cascade tables (macro_theses, sector_transmissions, stock_impacts,
mechanisms, cascade_liquidity). No writes, no DDL, no shell, no file access.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    """Requires psycopg2 (present in the Hermes venv)."""
    try:
        import psycopg2  # noqa: F401
        return True
    except Exception:
        return False


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return True


def interactive_setup() -> None:
    print("Cascade Query (Quark): a scoped read-only tool for the Cascade v2 engine output.")
    print("Nothing to configure interactively — reads the harvest7 Postgres cascade tables.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("cascade_query plugin register failed: %s", e)
