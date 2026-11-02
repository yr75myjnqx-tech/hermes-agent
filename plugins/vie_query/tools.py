"""
vie_query tools — let Quark read the VIE (Value Investor's Edge) Google Sheets.

Read-only Google Sheets values().get() calls against the fixed VIE spreadsheet
IDs. Uses the shared ~/.hermes/google_token.json (Google OAuth for the
niccarter59@gmail.com account). No writes are ever issued.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VIE spreadsheet IDs (from the vie-sheets skill)
# ---------------------------------------------------------------------------
_VIE_MAIN = "1gRGAetncvYO_iI_DAxTxO_Exx7z6YS4KOKD4pY3IVMs"  # Live Analytics Platform
_VIE_MODEL_PORTFOLIOS = "1rM8HTTk5vwmZi2CgkEni7t4V5CJOA0ODsFK06aihc54"
_VIE_MARKET_SCENARIOS = "1lMVj7_dz8FNbh_5hMdxxTx4YKOqdcTSpa4cWTnOMYfM"
_VIE_PREFERRED = "1ScnQHWI9Q1Y0DoAapgQLrrt-h5LKiMK_zkhcFKOcwkY"

_SHEET_NAMES = {
    "main": _VIE_MAIN,
    "live": _VIE_MAIN,
    "model_portfolios": _VIE_MODEL_PORTFOLIOS,
    "market_scenarios": _VIE_MARKET_SCENARIOS,
    "preferred": _VIE_PREFERRED,
}

# Key tabs on the main Live Analytics Platform sheet
_MAIN_TABS = {
    "Shipping Coverage Universe": "Shipping Coverage Universe",
    "Dry Bulk (Live)": "Dry Bulk (Live)",
    "Crude Tankers (Live)": "Crude Tankers (Live)",
    "Product Tankers (Live)": "Product Tankers (Live)",
    "Container Tracker (Live)": "Container Tracker (Live)",
    "LPG Tracker (Live)": "LPG Tracker (Live)",
    "LNG Tracker (Live)": "LNG Tracker (Live)",
    "Car Carrier Tracker (Live)": "Car Carrier Tracker (Live)",
    "Market Rates & Trends": "Market Rates & Trends",
    "Market Rate Charts": "Market Rate Charts",
    "Bunker Fuel Spreads": "Bunker Fuel Spreads",
    "Market Rates hist. data": "Market Rates hist. data",
    "Historical NAV discounts": "Historical NAV discounts",
    "Top Value List": "Top Value List",
    "Market Scenario Editor": "Market Scenario Editor",
    "Company data": "Company data",
    "Company Inputs": "Company Inputs",
    "Fleet Lists": "Fleet Lists",
    "VV Import": "VV Import",
    "Prices": "Prices",
}

_COVERAGE_TAB = "Shipping Coverage Universe"


# ---------------------------------------------------------------------------
# Google Sheets client
# ---------------------------------------------------------------------------
def _get_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = os.path.expanduser("~/.hermes/google_token.json")
    cred = Credentials.from_authorized_user_file(token_path)
    return build("sheets", "v4", credentials=cred, cache_discovery=False)


def _read_range(sheet_id: str, range_str: str) -> list:
    """Read a range from a sheet. Returns list of rows (list of cells)."""
    svc = _get_client()
    res = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_str
    ).execute()
    return res.get("values", [])


def _norm(s) -> str:
    return re.sub(r"[^0-9.\-]", "", s or "")


# ---------------------------------------------------------------------------
# Core query logic
# ---------------------------------------------------------------------------
def vie_lookup(args: dict, **_: Any) -> str:
    """Look up one symbol in the VIE Shipping Coverage Universe."""
    sym = str(args.get("symbol") or args.get("sym") or "").strip().upper().lstrip("$")
    if not sym:
        return "Error: 'symbol' is required (e.g. TEN, TRMD, SBLK)."
    rows = _read_range(_VIE_MAIN, f"'{_COVERAGE_TAB}'!A2:W300")
    for r in rows:
        if len(r) > 1 and r[1].upper().lstrip("$") == sym:
            return (
                f"{r[1]} | {r[0] if len(r)>0 else ''} | {r[2] if len(r)>2 else ''} | "
                f"Stance={r[3] if len(r)>3 else ''} | Price={r[5] if len(r)>5 else ''} | "
                f"Yield={r[6] if len(r)>6 else ''} | FVE={r[8] if len(r)>8 else ''} | "
                f"Upside={r[9] if len(r)>9 else ''} | Report={r[4] if len(r)>4 else ''}"
            )
    return f"{sym}: NOT in VIE shipping coverage universe (may be non-shipping/OSV or uncovered)."


def vie_universe(args: dict, **_: Any) -> str:
    """Dump the full compact VIE Shipping Coverage Universe."""
    rows = _read_range(_VIE_MAIN, f"'{_COVERAGE_TAB}'!A2:W300")
    lines = []
    for r in rows:
        if len(r) > 1 and r[1]:
            lines.append(
                f"{r[1]:6} {r[3] if len(r)>3 else '':9} "
                f"px={r[5] if len(r)>5 else '':10} FVE={r[8] if len(r)>8 else '':10} "
                f"up={(r[9] if len(r)>9 else '') or '-':8} "
                f"52wk={(r[22] if len(r)>22 else '') or '-':6} {r[0] if len(r)>0 else ''}"
            )
    if not lines:
        return "VIE coverage universe returned 0 rows."
    return "\n".join(lines) + f"\n({len(lines)} names)"


def vie_stance(args: dict, **_: Any) -> str:
    """Stance summary counts across the VIE coverage universe."""
    from collections import Counter
    rows = _read_range(_VIE_MAIN, f"'{_COVERAGE_TAB}'!A2:W300")
    c = Counter((r[3] if len(r) > 3 else "") for r in rows if len(r) > 1 and r[1])
    if not c:
        return "VIE coverage universe returned 0 rows."
    return "\n".join(f"{k:10} {v}" for k, v in c.most_common())


def vie_tab(args: dict, **_: Any) -> str:
    """Read a named tab from the VIE Live Analytics Platform (or another VIE sheet)."""
    tab = str(args.get("tab") or args.get("name") or "").strip()
    if not tab:
        return "Error: 'tab' is required (e.g. 'Dry Bulk (Live)', 'Top Value List', 'Crude Tankers (Live)')."
    sheet = str(args.get("sheet") or "main").strip().lower()
    sheet_id = _SHEET_NAMES.get(sheet, _VIE_MAIN)
    max_rows = int(args.get("max_rows") or 60)
    max_rows = max(1, min(max_rows, 200))
    # Resolve the tab name against known tabs if it's a key, else use as-is
    tab_name = _MAIN_TABS.get(tab, tab)
    range_str = f"'{tab_name}'!A1:Z{max_rows}"
    try:
        rows = _read_range(sheet_id, range_str)
    except Exception as e:  # pragma: no cover
        return f"VIE tab read failed: {e}"
    if not rows:
        return f"VIE tab '{tab_name}' returned 0 rows."
    lines = []
    for r in rows[:max_rows]:
        cells = [str(c) for c in r]
        lines.append(" | ".join(cells))
    return "\n".join(lines) + f"\n({len(rows)} rows from tab '{tab_name}')"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_SCHEMAS: Dict[str, dict] = {
    "vie_lookup": {
        "type": "function",
        "function": {
            "name": "vie_lookup",
            "description": (
                "Look up one shipping/offshore symbol in the VIE (Value Investor's Edge) "
                "Shipping Coverage Universe. Returns Firm, Stance (Bullish/Watch/Avoid), "
                "Price, Yield, Fair Value Estimate, Potential Upside, and latest report. "
                "Use before finalizing any shipping/offshore valuation, P/NAV, or exit/hold call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol (e.g. TEN, TRMD, SBLK, FRO)."}
                },
                "required": ["symbol"],
            },
        },
    },
    "vie_universe": {
        "type": "function",
        "function": {
            "name": "vie_universe",
            "description": (
                "Dump the full compact VIE Shipping Coverage Universe: every covered name with "
                "Stance, Price, Fair Value Estimate, Upside, and 52-week return."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "vie_stance": {
        "type": "function",
        "function": {
            "name": "vie_stance",
            "description": "Stance summary counts (Bullish / Watch / Avoid) across the VIE coverage universe.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "vie_tab": {
        "type": "function",
        "function": {
            "name": "vie_tab",
            "description": (
                "Read a named tab from the VIE Live Analytics Platform (or another VIE sheet). "
                "Tabs include: 'Dry Bulk (Live)', 'Crude Tankers (Live)', 'Product Tankers (Live)', "
                "'Container Tracker (Live)', 'LPG Tracker (Live)', 'LNG Tracker (Live)', "
                "'Car Carrier Tracker (Live)', 'Market Rates & Trends', 'Historical NAV discounts', "
                "'Top Value List', 'Market Scenario Editor', 'Company data', 'Fleet Lists', 'Prices'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab": {"type": "string", "description": "Tab name to read (e.g. 'Dry Bulk (Live)')."},
                    "sheet": {"type": "string", "description": "Sheet: 'main' (default), 'model_portfolios', 'market_scenarios', 'preferred'."},
                    "max_rows": {"type": "integer", "description": "Max rows to return (1-200, default 60)."},
                },
                "required": ["tab"],
            },
        },
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "vie_lookup": vie_lookup,
    "vie_universe": vie_universe,
    "vie_stance": vie_stance,
    "vie_tab": vie_tab,
}


def register_tools(ctx) -> None:
    """Register the vie_* tools in the ``vie_query`` toolset."""
    emoji = "\U0001f4ca"  # bar chart
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="vie_query",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
