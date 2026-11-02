"""
market_prices tools — lets Herb fetch live market prices for tickers.

Backend: a tiny yfinance fetcher living in a dedicated, isolated venv
(``~/.hermes/venvs/marketprices/``) that the plugin shells out to. This is the
same proven yfinance path the ``market-signal-report`` skill documents; hosting
it in an isolated venv keeps the plugin dependency-free and never touches
Hermes' own venv. Stdlib-only in this file (subprocess).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

_FETCH = "/Users/minipro/.hermes/venvs/marketprices/bin/python"
_FETCH_SCRIPT = "/Users/minipro/.hermes/venvs/marketprices/fetch_quotes.py"

# Allowed ticker chars (avoid SSRF / injection). Letters, digits, dot, dash, caret.
_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-^")
_MAX_HISTORY = 365


def _clean_sym(sym: str) -> str:
    """Normalise + validate a single ticker symbol. Returns '' if invalid."""
    sym = (sym or "").strip().upper().replace(" ", "")
    if not sym or len(sym) > 20 or any(ch not in _ALLOWED for ch in sym):
        return ""
    bad = ("..", "/", "http", "%", "@")
    if any(b in sym for b in bad):
        return ""
    return sym


def _call(spec: dict, timeout: int = 60) -> dict | str:
    """Run the yfinance fetcher with a JSON spec; return parsed dict or error str."""
    try:
        proc = subprocess.run(
            [_FETCH, _FETCH_SCRIPT],
            input=json.dumps(spec),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "price fetch timed out"}
    except Exception as e:  # pragma: no cover
        return {"error": f"price fetch failed to start: {e}"}
    out = proc.stdout.strip()
    if not out:
        return {"error": f"fetch returned no output (stderr: {proc.stderr[:200]})"}
    try:
        return json.loads(out)
    except Exception:
        return {"error": f"fetch returned bad JSON: {out[:200]}"}


def _fmt(x: Any) -> str:
    if x is None:
        return "--"
    try:
        return f"{float(x):,.2f}"
    except Exception:
        return str(x)


def _pct(cur: Any, prev: Any) -> str:
    try:
        if cur is None or prev is None or float(prev) == 0:
            return "--"
        return f"{((float(cur) / float(prev)) - 1) * 100:+.2f}%"
    except Exception:
        return "--"


def _format_quote(q: dict) -> List[str]:
    sym = q.get("symbol", "?")
    name = q.get("name") or ""
    cur = q.get("currency") or ""
    price = q.get("price")
    hp = q.get("fiftyTwoWeekHigh")
    lp = q.get("fiftyTwoWeekLow")
    lines = [
        f"📊 {sym} ({name})  ·  {cur}",
        f"   price: {_fmt(price)}",
        f"   52wk: {_fmt(lp)} – {_fmt(hp)}",
    ]
    hist = q.get("history") or []
    if len(hist) >= 2:
        prev = hist[-2]
        lines[1] += f"   (day {_pct(price, prev)})"
    return lines


def _history_lines(sym: str, hist: List[Any], n: int) -> List[str]:
    tail = hist[-n:] if n else hist
    rows = [f"\n📈 {sym} — last {len(tail)} sessions (close):"]
    for i, c in enumerate(tail):
        rows.append(f"   {i+1}. {_fmt(c)}")
    return rows


def market_price(args: dict, **_: Any) -> str:
    """Live quote for ONE ticker (price, day %, 52-week range, optional history)."""
    sym = str(args.get("symbol") or args.get("ticker") or "").strip()
    if not sym:
        return "Error: 'symbol' is required (e.g. SBLK, TEN, YAL.AX, TGLOF)."
    clean = _clean_sym(sym)
    if not clean:
        return f"Error: '{sym}' is not a valid ticker."
    hist = max(0, min(int(args.get("history") or 0), _MAX_HISTORY))
    res = _call({"kind": "price", "symbol": clean, "history": hist})
    if isinstance(res, dict) and res.get("error"):
        return f"Error: {res['error']}"
    if isinstance(res, str):
        return res
    lines = _format_quote(res)
    if hist:
        lines += _history_lines(clean, res.get("history") or [], hist)
    lines.append("\nSource: Yahoo Finance via yfinance (live).")
    return "\n".join(lines)


def market_quotes(args: dict, **_: Any) -> str:
    """Batch live quotes for multiple tickers in one call."""
    raw = args.get("symbols") or args.get("tickers") or ""
    if isinstance(raw, str):
        syms = [s for s in (x.strip() for x in raw.replace(";", ",").split(",")) if s]
    elif isinstance(raw, list):
        syms = [str(s).strip() for s in raw if str(s).strip()]
    else:
        syms = []
    if not syms:
        return "Error: 'symbols' is required (comma-separated, e.g. 'SBLK,TEN,GLNG,CMBT')."
    clean_syms = [_clean_sym(s) for s in syms][:25]
    valid = [s for s in clean_syms if s]
    invalid = [s for s, c in zip(syms, clean_syms) if not c]
    out: List[str] = []
    if invalid:
        out.append(f"⚠️ invalid symbols (skipped): {', '.join(invalid)}")
    if valid:
        res = _call({"kind": "quotes", "symbols": valid})
        if isinstance(res, list):
            for q in res:
                if q.get("error"):
                    out.append(f"⚠️ {q.get('symbol')}: {q['error']}")
                else:
                    out += _format_quote(q)
        else:
            err = res.get("error") if isinstance(res, dict) else res
            out.append(f"Error: {err}")
    if not valid:
        out.append("Error: no valid symbols to query.")
    out.append("\nSource: Yahoo Finance via yfinance (live).")
    return "\n".join(out)


def market_history(args: dict, **_: Any) -> str:
    """Daily close history for one ticker over a window."""
    sym = str(args.get("symbol") or args.get("ticker") or "").strip()
    if not sym:
        return "Error: 'symbol' is required."
    clean = _clean_sym(sym)
    if not clean:
        return f"Error: '{sym}' is not a valid ticker."
    days = max(1, min(int(args.get("days") or 90), _MAX_HISTORY))
    res = _call({"kind": "history", "symbol": clean, "days": days})
    if isinstance(res, dict) and res.get("error"):
        return f"Error: {res['error']}"
    if isinstance(res, str):
        return res
    out = _history_lines(clean, res.get("history") or [], days)
    out.append("\nSource: Yahoo Finance via yfinance (live).")
    return "\n".join(out)


_SCHEMAS: Dict[str, dict] = {
    "market_price": {
        "type": "function",
        "function": {
            "name": "market_price",
            "description": "Live quote for ONE ticker: price, day change, 52-week range, and optional recent-close history. Use for a single-name check.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker, e.g. SBLK, TEN, GLNG, CMBT, YAL.AX (ASX uses .AX), or OTC e.g. TGLOF."},
                    "history": {"type": "integer", "description": "Optional number of recent daily closes to include (0-365, default 0 = none)."},
                },
                "required": ["symbol"],
            },
        },
    },
    "market_quotes": {
        "type": "function",
        "function": {
            "name": "market_quotes",
            "description": "Batch live quotes for 2-25 tickers in one call. Returns price, day change, 52-week range for each. Use for a basket of holdings in one shot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "Comma-separated tickers, e.g. 'SBLK,TEN,GLNG,CMBT,YAL.AX'."},
                },
                "required": ["symbols"],
            },
        },
    },
    "market_history": {
        "type": "function",
        "function": {
            "name": "market_history",
            "description": "Daily close history for one ticker over N days (1-365). Use for sparklines / trend over time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker, e.g. SBLK."},
                    "days": {"type": "integer", "description": "Number of daily closes (1-365, default 90)."},
                },
                "required": ["symbol"],
            },
        },
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "market_price": market_price,
    "market_quotes": market_quotes,
    "market_history": market_history,
}


def register_tools(ctx) -> None:
    """Register the market_* tools in the ``market_prices`` toolset."""
    emoji = "\U0001f4c8"  # chart increasing
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="market_prices",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
