"""
market_prices plugin (HerbAI)

Registers read-only market-price tools in the ``market_prices`` toolset so the
HerbAI profile can fetch live closing prices, quotes and 52-week context for
shipping / energy / coal / commodity tickers itself, without needing
terminal/file/code_execution access.

Why this exists: Hermes deliberately denies Herb the terminal, file and
code_execution toolsets (scoped sandbox). The ``market-signal-report`` skill
ships a yfinance recipe, but Herb can never run it, so he falls back to
scraping (Yahoo blocks, Google CAPTCHAs him). This plugin restores price
access as a *scoped, read-only* tool that GETs Yahoo Finance's public edge
(``query1.finance.yahoo.com``) over HTTPS with just the stdlib. It grants NO
shell, NO file access, NO writes — only live market data reads.

Safety: the tool issues only HTTPS GETs to query1.finance.yahoo.com v8 chart
endpoints (no auth crumbs needed). Nothing else. No external packages
(url/pyyaml/requests not required) so it works in any venv.
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
    print("market_prices (HerbAI): a scoped read-only market-data tool for the herbai profile.")
    print("Nothing to configure interactively — endpoint is fixed to Yahoo Finance public chart API.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("market_prices plugin register failed: %s", e)
