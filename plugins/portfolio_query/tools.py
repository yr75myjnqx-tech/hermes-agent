"""
Portfolio Query plugin (Quark)

Registers a ``portfolio_query`` toolset so the Quark profile can READ the local
portfolio stores:
  - ~/portfolio/data/portfolio.db  (positions: qty, owner, cost basis, status)
  - ~/portfolio/data/ticker.db     (prices: bi-temporal price_records)

Why this exists: Hermes deliberately denies Quark the terminal, file, and
code_execution toolsets (scoped sandbox). The portfolio SoT lives in local
SQLite, which Quark could otherwise never query. This plugin restores that
capability as a *scoped, read-only* tool. It grants NO shell, NO file access,
NO writes to either database.

Safety: the tools open SQLite in read-only URI mode and run only fixed,
parameterised SELECT queries. Nothing else. Portfolio data never leaves the
machine (privacy rule: holdings stay local).
"""

from __future__ import annotations

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

__all__ = ["register"]

_PORTFOLIO_DB = os.path.expanduser("~/portfolio/data/portfolio.db")
_TICKER_DB = os.path.expanduser("~/portfolio/data/ticker.db")


def _pcon():
    con = sqlite3.connect(f"file:{_PORTFOLIO_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tcon():
    con = sqlite3.connect(f"file:{_TICKER_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _latest_price(tcon, ticker):
    """Latest price across any instrument key whose ticker matches (incl. .AX variants)."""
    r = tcon.execute(
        """SELECT pr.value, pr.raw_date FROM price_records pr
           JOIN instruments i ON pr.instrument = i.key
           WHERE i.ticker = ? OR i.ticker = ? || '.AX'
           ORDER BY pr.raw_date DESC LIMIT 1""",
        (ticker, ticker),
    ).fetchone()
    return (r["value"], r["raw_date"]) if r else (None, None)


# ---------------------------------------------------------------------------
# Tool implementations (each returns a compact text table)
# ---------------------------------------------------------------------------

def portfolio_positions(args: dict) -> str:
    owner = args.get("owner")
    ticker = args.get("ticker")
    q = "SELECT ticker, name, owner, qty, native_ccy, native_cost_per_share, sleeve, status FROM positions WHERE qty > 0"
    params = []
    if owner:
        q += " AND owner = ?"
        params.append(owner)
    if ticker:
        q += " AND ticker = ?"
        params.append(ticker.upper())
    q += " ORDER BY ticker, owner"
    con = _pcon()
    try:
        rows = con.execute(q, params).fetchall()
    finally:
        con.close()
    if not rows:
        return "No positions matched."
    lines = ["ticker | name | owner | qty | ccy | avg_cost | sleeve | status"]
    for r in rows:
        lines.append(
            f"{r['ticker']} | {(r['name'] or '')[:32]} | {r['owner']} | {r['qty']:,.0f} | "
            f"{r['native_ccy']} | {r['native_cost_per_share'] if r['native_cost_per_share'] is not None else '—'} | "
            f"{r['sleeve']} | {r['status']}"
        )
    return "\n".join(lines) + f"\n({len(rows)} position rows)"


def portfolio_top(args: dict) -> str:
    """Aggregate holdings by ticker (all owners), value in native ccy."""
    con = _pcon()
    tcon = _tcon()
    try:
        rows = con.execute(
            """SELECT ticker, MAX(name) AS name, SUM(qty) AS qty,
                      GROUP_CONCAT(DISTINCT owner) AS owners, native_ccy
               FROM positions WHERE qty > 0 GROUP BY ticker, native_ccy"""
        ).fetchall()
        agg = []
        for r in rows:
            px, pdate = _latest_price(tcon, r["ticker"])
            mv = (px * r["qty"]) if px else None
            agg.append((r["ticker"], (r["name"] or "")[:30], r["qty"], px, pdate,
                        mv, r["native_ccy"], r["owners"]))
        agg.sort(key=lambda x: -(x[5] or 0))
        limit = min(int(args.get("limit", 15)), 40)
        lines = ["ticker | name | qty | px | px_date | mkt_value | ccy | owners"]
        for t, name, qty, px, pdate, mv, ccy, owners in agg[:limit]:
            lines.append(f"{t} | {name} | {qty:,.0f} | {px if px else '—'} | {pdate or '—'} | "
                         f"{mv:,.0f} | {ccy} | {owners}")
        total = sum(x[5] or 0 for x in agg)
        lines.append(f"(top {min(limit, len(agg))} of {len(agg)} tickers; summed native-ccy market value {total:,.0f} — mixed currencies, do not sum across ccy)")
        return "\n".join(lines)
    finally:
        con.close()
        tcon.close()


def portfolio_nav(args: dict) -> str:
    """Total market value per currency, plus cost-basis-incomplete count."""
    con = _pcon()
    tcon = _tcon()
    try:
        rows = con.execute(
            "SELECT ticker, qty, native_ccy FROM positions WHERE qty > 0"
        ).fetchall()
        totals = {}
        incomplete = []
        for r in rows:
            px, _ = _latest_price(tcon, r["ticker"])
            if px is None:
                incomplete.append(r["ticker"])
                continue
            totals[r["native_ccy"]] = totals.get(r["native_ccy"], 0.0) + px * r["qty"]
        lines = ["ccy | market_value"]
        for ccy, v in sorted(totals.items()):
            lines.append(f"{ccy} | {v:,.2f}")
        if incomplete:
            lines.append(f"UNPRICED: {', '.join(sorted(set(incomplete)))}")
        return "\n".join(lines)
    finally:
        con.close()
        tcon.close()


def portfolio_price(args: dict) -> str:
    ticker = (args.get("ticker") or "").upper()
    if not ticker:
        return "ticker required"
    tcon = _tcon()
    try:
        px, pdate = _latest_price(tcon, ticker)
        if px is None:
            return f"No price for {ticker} in ticker.db."
        return f"{ticker}: {px} (as of {pdate})"
    finally:
        tcon.close()


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------
_SCHEMAS = {
    "portfolio_positions": {
        "type": "function",
        "function": {
            "name": "portfolio_positions",
            "description": (
                "List portfolio holdings (read-only) from the local portfolio.db: ticker, "
                "name, owner (Nic/Sue/CFSF/Morgan), qty, native currency, average cost, "
                "sleeve, status. Filter by owner and/or ticker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Filter by owner: Nic, Sue, CFSF, Morgan."},
                    "ticker": {"type": "string", "description": "Filter by ticker symbol (e.g. TEN)."},
                },
            },
        },
    },
    "portfolio_top": {
        "type": "function",
        "function": {
            "name": "portfolio_top",
            "description": (
                "Aggregate portfolio holdings by ticker across all owners (read-only), with "
                "latest price and market value in the position's native currency. Sorted by "
                "value descending. NOTE: values are mixed-currency — do not sum across currencies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (1-40, default 15)."},
                },
            },
        },
    },
    "portfolio_nav": {
        "type": "function",
        "function": {
            "name": "portfolio_nav",
            "description": (
                "Total portfolio market value per native currency (read-only), from live "
                "positions priced via ticker.db. Lists any unpriced tickers."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "portfolio_price": {
        "type": "function",
        "function": {
            "name": "portfolio_price",
            "description": "Latest stored price for one ticker from ticker.db (read-only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol (e.g. TEN)."},
                },
                "required": ["ticker"],
            },
        },
    },
}

_HANDLERS = {
    "portfolio_positions": portfolio_positions,
    "portfolio_top": portfolio_top,
    "portfolio_nav": portfolio_nav,
    "portfolio_price": portfolio_price,
}


def register_tools(ctx) -> None:
    emoji = "\U0001f4bc"  # briefcase
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="portfolio_query",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
