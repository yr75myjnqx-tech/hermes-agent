"""
Portfolio Query plugin (Quark)

Registers a ``portfolio_query`` toolset so the Quark profile can READ the local
portfolio stores (~/portfolio/data/portfolio.db holdings + ticker.db prices)
without needing terminal/file/code_execution access.

Scoped, READ-ONLY: SQLite opened in read-only URI mode, fixed parameterised
SELECTs only. No shell, no file access, no writes. Portfolio data never
leaves the machine (privacy rule: holdings stay local).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    return os.path.exists(os.path.expanduser("~/portfolio/data/portfolio.db"))


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return check_requirements()


def interactive_setup() -> None:
    print("Portfolio Query (Quark): scoped read-only tools over ~/portfolio/data/*.db")
    print("Requires ~/portfolio/data/portfolio.db + ticker.db to exist.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("portfolio_query plugin register failed: %s", e)
